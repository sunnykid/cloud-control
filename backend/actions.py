"""EC2 시작·중지 처리. 학습용 단일 프로세스 앱 기준으로 프로세스 전역 lock 하나로 직렬화한다."""

import threading
from dataclasses import dataclass

from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)
from sqlalchemy.exc import SQLAlchemyError

from backend import action_store as store
from backend.auth import ensure_controllable
from backend.aws_service import AwsService, translate_error
from backend.config import ALLOWED_REGION, Settings
from backend.models import STATUS_ACCEPTED, STATUS_FAILED, STATUS_UNKNOWN

# 프로세스에서 한 번만 만든다. 모든 변경 진입점은 이 lock을 사용한다.
CONTROL_LOCK = threading.Lock()

# 현재 상태 → 허용 작업
REQUIRED_STATE = {"start": "stopped", "stop": "running"}

# 요청이 AWS로 전송되지 않았음이 확실한 자격증명 오류
_NOT_SENT_ERRORS = (
    LoginError,
    SSOError,
    TokenRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    ProfileNotFound,
)


class ActionError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class ActionResult:
    created: bool
    action_id: str
    request_id: str
    status: str


def _db(func, *args):
    """DB 실패는 503으로 바꾼다. 이 오류가 나면 AWS 변경 호출로 진행하지 않는다."""
    try:
        return func(*args)
    except SQLAlchemyError as exc:
        raise ActionError(503, "DB_UNAVAILABLE", "DB 처리에 실패했습니다. AWS 변경 요청은 보내지 않았습니다.") from exc


def _replay(existing: dict, instance_id: str, action: str) -> ActionResult:
    """같은 request_id: 같은 내용이면 기존 결과를 돌려주고, 다른 내용이면 409."""
    if existing["instance_id"] != instance_id or existing["action"] != action:
        raise ActionError(409, "IDEMPOTENCY_CONFLICT", "같은 요청 키가 다른 대상 또는 작업에 이미 사용되었습니다.")
    return ActionResult(False, existing["id"], existing["request_id"], existing["status"])


def request_action(settings: Settings, instance_id: str, action: str, request_id: str) -> ActionResult:
    if action not in REQUIRED_STATE:
        raise ActionError(422, "VALIDATION_ERROR", "action은 start 또는 stop만 허용합니다.")
    if settings.aws_region != ALLOWED_REGION:
        raise ActionError(500, "CONFIG_ERROR", "허용되지 않은 리전 설정입니다.")
    # 서버 허용 목록 밖이면 AWS·DB 호출 없이 403.
    ensure_controllable(settings, instance_id)

    with CONTROL_LOCK:
        # 1) 중복 키 판정은 EC2 상태 검사보다 먼저 한다.
        existing = _db(store.get_by_request_id, request_id)
        if existing:
            return _replay(existing, instance_id, action)

        # 2) 같은 대상에 미확정(REQUESTED/UNKNOWN) 이력이 있으면 거절한다.
        open_action = _db(store.find_open_action, instance_id)
        if open_action:
            raise ActionError(
                409,
                "ACTION_IN_PROGRESS",
                f"이 인스턴스에 결과가 확정되지 않은 작업({open_action['status']})이 있어 새 제어를 거절합니다.",
            )

        # 3) 예상 계정과 최신 EC2 상태를 lock 안에서 확인한다.
        aws = AwsService(settings)
        aws.get_context()
        state = aws.get_instance_state(instance_id)
        if state != REQUIRED_STATE[action]:
            raise ActionError(
                409,
                "INVALID_INSTANCE_STATE",
                f"현재 상태({state})에서는 {action}을 할 수 없습니다. {action}은 {REQUIRED_STATE[action]} 상태에서만 가능합니다.",
            )

        # 4) REQUESTED를 commit한 뒤에만 AWS를 호출한다.
        try:
            record = _db(store.create_requested, request_id, instance_id, action)
        except store.DuplicateRequestId as exc:
            # UNIQUE 충돌: 다른 경로가 먼저 저장했다. 기존 이력으로 판정한다.
            existing = _db(store.get_by_request_id, request_id)
            if existing is None:
                raise ActionError(503, "DB_UNAVAILABLE", "요청 키 충돌을 확인하지 못했습니다.") from exc
            return _replay(existing, instance_id, action)

        return _call_aws(aws, record, instance_id, action)


def _call_aws(aws: AwsService, record: dict, instance_id: str, action: str) -> ActionResult:
    ids = {"action_id": record["id"], "request_id": record["request_id"]}
    error: ActionError | None = None
    aws_request_id = None
    try:
        client = aws.control_client()
        if action == "start":
            resp = client.start_instances(InstanceIds=[instance_id])
            change = (resp.get("StartingInstances") or [{}])[0]
        else:
            resp = client.stop_instances(InstanceIds=[instance_id])
            change = (resp.get("StoppingInstances") or [{}])[0]
        status = STATUS_ACCEPTED
        aws_request_id = resp.get("ResponseMetadata", {}).get("RequestId")
        message = f"{change.get('PreviousState', {}).get('Name', '?')} -> {change.get('CurrentState', {}).get('Name', '?')}"
    except ClientError as exc:
        aws_request_id = exc.response.get("ResponseMetadata", {}).get("RequestId")
        code = exc.response.get("Error", {}).get("Code", "")
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500:
            # AWS 내부 오류는 실제 처리 여부를 알 수 없다.
            status, message = STATUS_UNKNOWN, f"AWS 서버 오류: {code}"
            error = _unknown_error(ids)
        else:
            status, message = STATUS_FAILED, f"AWS 거절: {code}"
            translated = translate_error(exc)
            error = ActionError(translated.status_code, translated.code, translated.message, **ids, status=status)
    except _NOT_SENT_ERRORS as exc:
        status, message = STATUS_FAILED, f"요청 전송 전 자격증명 오류: {type(exc).__name__}"
        translated = translate_error(exc)
        error = ActionError(translated.status_code, translated.code, translated.message, **ids, status=status)
    except Exception as exc:  # 타임아웃·연결 끊김 등: 접수 여부를 알 수 없다. 재시도하지 않는다.
        status, message = STATUS_UNKNOWN, f"결과 불확실: {type(exc).__name__}"
        error = _unknown_error(ids)

    try:
        store.mark_result(record["id"], status, aws_request_id, message)
    except SQLAlchemyError:
        # REQUESTED는 삭제하지 않고 남겨 같은 대상의 새 제어를 막는다. AWS는 재호출하지 않는다.
        raise ActionError(
            503,
            "DB_UPDATE_FAILED",
            f"AWS 호출 결과({status})를 이력에 반영하지 못했습니다. 이력은 REQUESTED로 남습니다.",
            **ids,
            status=status,
        )

    if error:
        raise error
    return ActionResult(True, record["id"], record["request_id"], status)


def _unknown_error(ids: dict) -> ActionError:
    return ActionError(
        503,
        "AWS_RESULT_UNKNOWN",
        "AWS 처리 결과를 확인하지 못했습니다. 자동 재시도하지 않습니다. EC2 상태를 새로고침해 확인하세요.",
        **ids,
        status=STATUS_UNKNOWN,
    )

