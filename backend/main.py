"""FastAPI 앱. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 설정을 검증한다. 실패하면 변수 이름만 담긴 메시지로 시작을 중단한다.
    get_settings()
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError):
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError):
    return error_response(500, "CONFIG_ERROR", str(exc))


def verified_service() -> AwsService:
    """모든 조회 전에 STS 계정이 EXPECTED_ACCOUNT_ID와 같은지 확인한다."""
    service = AwsService(get_settings())
    service.get_context()
    return service


# 동기 Boto3 호출이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@app.get("/api/context")
def get_context():
    return AwsService(get_settings()).get_context()


@app.get("/api/ec2")
def list_ec2(service: AwsService = Depends(verified_service)):
    items = service.list_ec2_instances()
    return {"items": items, "count": len(items)}


@app.get("/api/s3")
def list_s3(service: AwsService = Depends(verified_service)):
    items = service.list_s3_buckets()
    return {"items": items, "count": len(items)}


@app.get("/api/security-groups")
def list_security_groups(service: AwsService = Depends(verified_service)):
    items = service.list_security_groups()
    return {"items": items, "count": len(items)}


# API 경로 뒤에 frontend 폴더만 정적 파일로 공개한다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
