"""앱 런타임 설정. 서버가 실행될 때만 프로젝트 루트의 .env를 읽는다."""

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"

ALLOWED_REGION = "ap-northeast-2"
REQUIRED_KEYS = ("AWS_PROFILE", "AWS_REGION", "EXPECTED_ACCOUNT_ID", "ADMIN_API_TOKEN")


class ConfigError(Exception):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    expected_account_id: str
    admin_api_token: str = field(repr=False)
    allowed_instance_ids: frozenset[str] = frozenset()


def parse_id_list(raw: str) -> frozenset[str]:
    """쉼표로 나누고 앞뒤 공백과 빈 항목을 제거한 집합을 만든다."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str


DB_REQUIRED_KEYS = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")


@lru_cache
def get_db_settings() -> DbSettings:
    load_dotenv(ENV_FILE)

    values = {key: os.getenv(key, "").strip() for key in DB_REQUIRED_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 DB 설정이 없습니다: {', '.join(missing)}")

    port_raw = os.getenv("DB_PORT", "").strip() or "3306"
    if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
        raise ConfigError("DB_PORT는 1~65535 사이의 숫자여야 합니다.")

    return DbSettings(
        host=values["DB_HOST"],
        port=int(port_raw),
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
        database=values["DB_NAME"],
    )


@lru_cache
def get_settings() -> Settings:
    load_dotenv(ENV_FILE)

    values = {key: os.getenv(key, "").strip() for key in REQUIRED_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)}")

    if values["AWS_REGION"] != ALLOWED_REGION:
        raise ConfigError(f"AWS_REGION은 {ALLOWED_REGION}만 허용합니다.")

    if not re.fullmatch(r"\d{12}", values["EXPECTED_ACCOUNT_ID"]):
        raise ConfigError("EXPECTED_ACCOUNT_ID는 12자리 숫자여야 합니다.")

    return Settings(
        aws_profile=values["AWS_PROFILE"],
        aws_region=values["AWS_REGION"],
        expected_account_id=values["EXPECTED_ACCOUNT_ID"],
        admin_api_token=values["ADMIN_API_TOKEN"],
        allowed_instance_ids=parse_id_list(os.getenv("ALLOWED_INSTANCE_IDS", "")),
    )
