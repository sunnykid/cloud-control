"""DB 엔진과 요청별 세션. FastAPI 앱(main.py)을 import하지 않는다."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import get_db_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    s = get_db_settings()
    url = URL.create(
        drivername="mysql+pymysql",
        username=s.user,
        password=s.password,
        host=s.host,
        port=s.port,
        database=s.database,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10},
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """요청마다 세션을 열고 요청이 끝나면 닫는다."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def describe_db_error(exc: SQLAlchemyError) -> str:
    """URL·암호·SQL 파라미터를 제외하고 드라이버 오류 코드와 메시지만 돌려준다."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        orig = exc.orig
        return f"{type(orig).__name__}: {', '.join(str(a) for a in orig.args)}"
    return type(exc).__name__
