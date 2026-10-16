"""Boto3 조회 로직과 AWS 오류 분류."""

from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    CredentialRetrievalError,
    HTTPClientError,
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)

from backend.config import Settings
from backend.security_check import inspect_group

CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)
# 변경(시작·중지) 전용: 자동 재시도 없이 한 번만 보낸다.
CONTROL_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 1},
)

ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AllAccessDisabled",
}
LOGIN_REQUIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "InvalidToken",
    "RequestExpired",
    "AuthFailure",
    "UnrecognizedClientException",
}


# 점검 대상: 실제로 존재하는(종료되지 않은) 인스턴스
SCAN_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]
SECURITY_GROUP_BATCH = 100


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _access_denied() -> AwsServiceError:
    return AwsServiceError(403, "AWS_ACCESS_DENIED", "AWS 권한이 부족합니다.")


def _login_required() -> AwsServiceError:
    return AwsServiceError(
        503,
        "AWS_LOGIN_REQUIRED",
        "AWS 로그인이 만료되었거나 갱신할 수 없습니다. aws login --profile <프로필>을 다시 실행하세요.",
    )


def _scan_incomplete(reason: str) -> AwsServiceError:
    return AwsServiceError(
        503,
        "AWS_SCAN_INCOMPLETE",
        f"보안 그룹 점검을 완료하지 못했습니다({reason}). 잠시 후 다시 점검하세요.",
    )


def _connection_error() -> AwsServiceError:
    return AwsServiceError(503, "AWS_CONNECTION_ERROR", "AWS와 통신하지 못했습니다. 네트워크를 확인하세요.")


def translate_error(exc: Exception) -> AwsServiceError:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED_CODES:
            return _access_denied()
        if code in LOGIN_REQUIRED_CODES:
            return _login_required()
        if code.startswith("InvalidInstanceID"):
            return AwsServiceError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.")
        if code == "IncorrectInstanceState":
            return AwsServiceError(409, "INVALID_INSTANCE_STATE", "현재 인스턴스 상태에서는 요청한 작업을 할 수 없습니다.")
        return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청이 실패했습니다.")
    if isinstance(
        exc,
        (
            LoginError,
            SSOError,
            TokenRetrievalError,
            NoCredentialsError,
            PartialCredentialsError,
            CredentialRetrievalError,
            ProfileNotFound,
        ),
    ):
        return _login_required()
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return _connection_error()
    return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청이 실패했습니다.")


class AwsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            # 요청마다 새 세션을 만들어 aws login 재실행 결과를 바로 반영한다.
            self.session = boto3.Session(
                profile_name=settings.aws_profile,
                region_name=settings.aws_region,
            )
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc

    def _client(self, name: str):
        return self.session.client(name, config=CLIENT_CONFIG)

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc

    def get_context(self) -> dict:
        identity = self._call(self._client("sts").get_caller_identity)
        account_id = identity["Account"]
        if account_id != self.settings.expected_account_id:
            raise AwsServiceError(
                403,
                "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다.",
            )
        return {
            "account_id": account_id,
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
        }

    def _paginate(self, client, operation: str, result_key: str, **kwargs) -> list:
        def run():
            items = []
            for page in client.get_paginator(operation).paginate(**kwargs):
                items.extend(page.get(result_key, []))
            return items

        return self._call(run)

    def list_ec2_instances(self) -> list[dict]:
        reservations = self._paginate(self._client("ec2"), "describe_instances", "Reservations")
        items = []
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                items.append(
                    {
                        "instance_id": inst["InstanceId"],
                        "name": tags.get("Name") or inst["InstanceId"],
                        "state": inst.get("State", {}).get("Name"),
                        "instance_type": inst.get("InstanceType"),
                        "private_ip": inst.get("PrivateIpAddress"),
                        "public_ip": inst.get("PublicIpAddress"),
                        "controllable": inst["InstanceId"] in self.settings.allowed_instance_ids,
                    }
                )
        return items

    def get_instance_state(self, instance_id: str) -> str:
        """최신 EC2 상태를 조회한다(읽기 전용 client)."""
        resp = self._call(self._client("ec2").describe_instances, InstanceIds=[instance_id])
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                if inst.get("InstanceId") == instance_id:
                    return inst.get("State", {}).get("Name", "")
        raise AwsServiceError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.")

    def control_client(self):
        """시작·중지 전용 client. 오류 분류는 호출하는 쪽에서 직접 한다."""
        return self.session.client("ec2", config=CONTROL_CONFIG)

    def list_s3_buckets(self) -> list[dict]:
        buckets = self._paginate(self._client("s3"), "list_buckets", "Buckets")
        return [{"name": b["Name"]} for b in buckets]

    def list_security_groups(self) -> list[dict]:
        groups = self._paginate(self._client("ec2"), "describe_security_groups", "SecurityGroups")
        return [
            {
                "GroupId": g["GroupId"],
                "GroupName": g["GroupName"],
                "IpPermissions": g.get("IpPermissions", []),
            }
            for g in groups
        ]

    def scan_attached_security_groups(self) -> dict:
        """실제 EC2의 모든 ENI에 연결된 보안 그룹만 점검한다.

        DescribeInstances·DescribeSecurityGroups 조회만 사용한다. 제어 허용 목록(ALLOWED_INSTANCE_IDS)은
        시작·중지 제한이므로 점검 대상 필터로 쓰지 않는다. 일부라도 확인하지 못하면 오류로 끝낸다.
        """
        self.get_context()  # EXPECTED_ACCOUNT_ID 검증

        reservations = self._paginate(
            self._client("ec2"),
            "describe_instances",
            "Reservations",
            Filters=[{"Name": "instance-state-name", "Values": SCAN_INSTANCE_STATES}],
        )

        # (instance_id, network_interface_id, group_id) 기준으로 중복을 제거한다.
        links: dict[tuple[str, str, str], dict] = {}
        instance_ids: set[str] = set()
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                instance_id = inst.get("InstanceId")
                if not instance_id:
                    raise _scan_incomplete("인스턴스 ID 누락")
                instance_ids.add(instance_id)
                tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", [])}
                name = tags.get("Name") or instance_id
                state = inst.get("State", {}).get("Name")

                enis = inst.get("NetworkInterfaces")
                if not enis:
                    # 존재하는 EC2에는 ENI가 있어야 한다. 대상 0건으로 단정하지 않는다.
                    raise _scan_incomplete(f"{instance_id}의 네트워크 인터페이스 정보 누락")
                for eni in enis:
                    eni_id = eni.get("NetworkInterfaceId")
                    groups = eni.get("Groups")
                    if not eni_id or not groups:
                        raise _scan_incomplete(f"{instance_id}의 네트워크 인터페이스 보안 그룹 정보 누락")
                    for group in groups:
                        group_id = group.get("GroupId")
                        if not group_id:
                            raise _scan_incomplete(f"{instance_id}의 보안 그룹 ID 누락")
                        links[(instance_id, eni_id, group_id)] = {
                            "instance_id": instance_id,
                            "instance_name": name,
                            "state": state,
                            "network_interface_id": eni_id,
                        }

        group_ids = sorted({group_id for (_, _, group_id) in links})
        result = {
            "findings": [],
            "checked_groups": [],
            "checked_instance_count": len(instance_ids),
            "checked_group_count": 0,
            "finding_count": 0,
            "checked_at": None,
        }
        # 연결 그룹이 0개면 보안 그룹 조회를 호출하지 않는다(빈 GroupIds는 전체 조회가 되므로 금지).
        groups_by_id = {}
        if group_ids:
            client = self._client("ec2")
            for start in range(0, len(group_ids), SECURITY_GROUP_BATCH):
                batch = group_ids[start:start + SECURITY_GROUP_BATCH]
                for group in self._describe_security_group_batch(client, batch):
                    groups_by_id[group["GroupId"]] = group

        # 모든 조회가 성공한 뒤에만 결과를 만든다.
        for group_id in group_ids:
            group = groups_by_id[group_id]
            connections = sorted(
                (info for (_, _, gid), info in links.items() if gid == group_id),
                key=lambda c: (c["instance_id"], c["network_interface_id"]),
            )
            # AWS 원본 GroupId/GroupName/IpPermissions 구조 그대로 그룹마다 한 번만 점검한다.
            # finding은 그룹의 규칙·공개 CIDR 조합 단위이며 연결 인스턴스 수만큼 늘리지 않는다.
            for finding in inspect_group(group):
                result["findings"].append({**finding, "connections": [dict(c) for c in connections]})
            result["checked_groups"].append(
                {"group_id": group_id, "group_name": group.get("GroupName", ""), "connections": connections}
            )

        result["checked_group_count"] = len({g["group_id"] for g in result["checked_groups"]})
        result["finding_count"] = len(result["findings"])
        result["checked_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result

    def _describe_security_group_batch(self, client, batch: list[str]) -> list[dict]:
        if not batch:
            raise ValueError("빈 GroupIds로 보안 그룹을 조회하지 않는다.")
        try:
            resp = client.describe_security_groups(GroupIds=batch)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code.startswith("InvalidGroup"):
                # 조회 사이에 그룹이 삭제되는 등 대상이 바뀌었다.
                raise _scan_incomplete("조회 중 보안 그룹이 변경됨") from exc
            raise translate_error(exc) from exc
        except BotoCoreError as exc:
            raise translate_error(exc) from exc

        groups = resp.get("SecurityGroups", [])
        returned = {g.get("GroupId") for g in groups}
        if returned != set(batch) or resp.get("NextToken"):
            raise _scan_incomplete("요청한 보안 그룹과 반환된 보안 그룹이 다름")
        return groups
