"""FastAPI 앱. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.actions import ActionError, request_action
from backend.auth import require_app_token
from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, get_db_settings, get_settings
from backend.db import get_db
from backend.models import ActionLog

ACTION_LOG_LIMIT = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 검증한다. ADMIN_API_TOKEN·DB 설정 누락 등은 변수 이름만 담긴 메시지로 시작을 거절한다.
    get_settings()
    get_db_settings()
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError):
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ActionError)
def handle_action_error(request: Request, exc: ActionError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message, **exc.extra}},
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, exc: RequestValidationError):
    # 입력값을 되돌려 보내지 않고 문제 필드 위치만 안내한다.
    fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()})
    return error_response(422, "VALIDATION_ERROR", f"요청 형식이 올바르지 않습니다: {', '.join(fields)}")


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError):
    return error_response(500, "CONFIG_ERROR", str(exc))


@app.exception_handler(SQLAlchemyError)
def handle_db_error(request: Request, exc: SQLAlchemyError):
    # 응답·로그에 DB URL·암호·SQL을 넣지 않는다.
    if isinstance(exc, (OperationalError, InterfaceError)):
        return error_response(503, "DB_UNAVAILABLE", "DB에 연결할 수 없습니다. MariaDB 상태와 DB 설정을 확인하세요.")
    if isinstance(exc, ProgrammingError):
        return error_response(500, "DB_SCHEMA_ERROR", "DB 테이블이 없거나 구조가 다릅니다. init_db를 실행하세요.")
    return error_response(500, "DB_ERROR", "DB 처리 중 오류가 발생했습니다.")


def verified_service() -> AwsService:
    """모든 조회 전에 STS 계정이 EXPECTED_ACCOUNT_ID와 같은지 확인한다."""
    service = AwsService(get_settings())
    service.get_context()
    return service


# 라우터 의존성은 경로 의존성보다 먼저 실행되므로, 인증 실패 시 AWS를 호출하지 않는다.
# 경로들은 입력 파라미터를 받지 않아 클라이언트가 계정·리전·허용 목록을 바꿀 수 없다.
api = APIRouter(prefix="/api", dependencies=[Depends(require_app_token)])


# 동기 Boto3 호출이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@api.get("/context")
def get_context():
    return AwsService(get_settings()).get_context()


@api.get("/ec2")
def list_ec2(service: AwsService = Depends(verified_service)):
    items = service.list_ec2_instances()
    return {"items": items, "count": len(items)}


@api.get("/s3")
def list_s3(service: AwsService = Depends(verified_service)):
    items = service.list_s3_buckets()
    return {"items": items, "count": len(items)}


@api.get("/security-groups")
def list_security_groups(service: AwsService = Depends(verified_service)):
    items = service.list_security_groups()
    return {"items": items, "count": len(items)}


@api.get("/security-checks")
def security_checks():
    """매 요청마다 AWS에서 새로 조회해 점검한다. 결과는 저장하거나 캐시하지 않는다."""
    return AwsService(get_settings()).scan_attached_security_groups()


@api.get("/actions")
def list_actions(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ActionLog)
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .limit(ACTION_LOG_LIMIT)
    ).all()
    items = [row.to_dict() for row in rows]
    return {"items": items, "count": len(items)}


class ActionRequest(BaseModel):
    """action과 request_id만 받는다. 추가 필드·잘못된 UUID는 422."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]
    request_id: UUID


@api.post("/ec2/{instance_id}/actions")
def create_action(instance_id: str, body: ActionRequest, response: Response):
    result = request_action(get_settings(), instance_id, body.action, str(body.request_id))
    response.status_code = 202 if result.created else 200
    return {"action_id": result.action_id, "request_id": result.request_id, "status": result.status}


app.include_router(api)

# API 경로 뒤에 frontend 폴더만 정적 파일로 공개한다. 정적 파일은 인증 없이 열린다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
