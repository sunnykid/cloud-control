"""DB 모델. 시각은 모두 UTC 기준의 naive DATETIME으로 저장한다."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# 이력 상태: AWS 호출 전 REQUESTED, 접수 ACCEPTED, 명시적 거절 FAILED, 결과 불확실 UNKNOWN
STATUS_REQUESTED = "REQUESTED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_FAILED = "FAILED"
STATUS_UNKNOWN = "UNKNOWN"
OPEN_STATUSES = (STATUS_REQUESTED, STATUS_UNKNOWN)


def new_uuid() -> str:
    return str(uuid.uuid4())


class ActionLog(Base):
    __tablename__ = "action_logs"
    __table_args__ = (
        Index("ix_action_logs_created_at", "created_at"),
        UniqueConstraint("request_id", name="uq_action_logs_request_id"),
        Index("ix_action_logs_instance_status", "instance_id", "status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    aws_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "action": self.action,
            "status": self.status,
            "aws_request_id": self.aws_request_id,
            "created_at": self.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": self.message,
        }
