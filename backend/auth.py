"""앱 토큰 인증과 EC2 제어 허용 목록 검사."""

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def _auth_required() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "APP_AUTH_REQUIRED", "message": "앱 토큰이 없거나 올바르지 않습니다."},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_app_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """모든 /api 경로의 공통 인증. 누락·잘못된 scheme·빈 값·불일치는 모두 401."""
    # HTTPBearer(auto_error=False)는 헤더 누락, Bearer가 아닌 scheme, 빈 값이면 None을 돌려준다.
    if credentials is None or not credentials.credentials:
        raise _auth_required()

    expected = get_settings().admin_api_token
    try:
        # bytes로 비교해야 비ASCII 입력에서도 TypeError(500) 없이 비교된다.
        matched = secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, UnicodeError):
        matched = False
    if not matched:
        raise _auth_required()


def is_controllable(settings: Settings, instance_id: str) -> bool:
    return instance_id in settings.allowed_instance_ids


def ensure_controllable(settings: Settings, instance_id: str) -> None:
    """EC2 시작·중지 전에 호출한다. 허용 목록 밖이면 AWS 호출 전에 403으로 차단한다.

    허용 목록은 서버 설정(ALLOWED_INSTANCE_IDS)에서만 읽고, 비어 있으면 모든 제어를 거절한다.
    """
    if not settings.allowed_instance_ids:
        raise HTTPException(
            status_code=403,
            detail={"code": "INSTANCE_CONTROL_DISABLED", "message": "제어 허용 목록이 비어 있어 시작·중지를 할 수 없습니다."},
        )
    if not isinstance(instance_id, str) or not is_controllable(settings, instance_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "INSTANCE_NOT_ALLOWED", "message": "제어 허용 목록에 없는 인스턴스입니다."},
        )
