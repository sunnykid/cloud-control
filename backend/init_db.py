"""없는 테이블만 생성한다. 기존 테이블·데이터는 삭제하거나 변경하지 않는다.

실행: .venv/bin/python -m backend.init_db
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

import backend.models  # noqa: F401  모든 모델을 메타데이터에 등록한다.
from backend.config import ConfigError
from backend.db import Base, describe_db_error, get_engine


def main() -> int:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        existing = set(inspect(engine).get_table_names())
        Base.metadata.create_all(engine, checkfirst=True)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"DB 오류: {describe_db_error(exc)}", file=sys.stderr)
        return 1

    for name in Base.metadata.tables:
        print(f"{name}: {'이미 있음(유지)' if name in existing else '생성함'}")

    # 기존 테이블은 변경하지 않으므로, 중복 방지에 필요한 UNIQUE 제약이 없으면 알리고 실패로 끝낸다.
    try:
        if not _has_unique_request_id(engine):
            print(
                "action_logs.request_id에 UNIQUE 제약이 없습니다. 다음 SQL을 직접 실행하세요:\n"
                "  ALTER TABLE action_logs DROP INDEX ix_action_logs_request_id,\n"
                "    ADD CONSTRAINT uq_action_logs_request_id UNIQUE (request_id),\n"
                "    ADD INDEX ix_action_logs_instance_status (instance_id, status);",
                file=sys.stderr,
            )
            return 1
    except SQLAlchemyError as exc:
        print(f"DB 오류: {describe_db_error(exc)}", file=sys.stderr)
        return 1
    return 0


def _has_unique_request_id(engine) -> bool:
    insp = inspect(engine)
    for uc in insp.get_unique_constraints("action_logs"):
        if uc["column_names"] == ["request_id"]:
            return True
    for ix in insp.get_indexes("action_logs"):
        if ix.get("unique") and ix["column_names"] == ["request_id"]:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
