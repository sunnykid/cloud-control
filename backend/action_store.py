"""action_logs 저장 함수. 함수마다 짧은 세션을 열고 닫아 항상 최신 커밋을 읽는다."""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.db import get_sessionmaker
from backend.models import OPEN_STATUSES, STATUS_REQUESTED, ActionLog


class DuplicateRequestId(Exception):
    """request_id UNIQUE 제약 충돌."""


def get_by_request_id(request_id: str) -> dict | None:
    with get_sessionmaker()() as session:
        row = session.scalars(select(ActionLog).where(ActionLog.request_id == request_id)).first()
        return row.to_dict() if row else None


def find_open_action(instance_id: str) -> dict | None:
    """같은 대상의 REQUESTED 또는 UNKNOWN 이력을 찾는다."""
    with get_sessionmaker()() as session:
        row = session.scalars(
            select(ActionLog)
            .where(ActionLog.instance_id == instance_id, ActionLog.status.in_(OPEN_STATUSES))
            .order_by(ActionLog.created_at.desc())
        ).first()
        return row.to_dict() if row else None


def create_requested(request_id: str, instance_id: str, action: str) -> dict:
    """REQUESTED 이력을 commit한다. commit이 끝나야 AWS를 호출할 수 있다."""
    with get_sessionmaker()() as session:
        row = ActionLog(request_id=request_id, instance_id=instance_id, action=action, status=STATUS_REQUESTED)
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise DuplicateRequestId() from exc
        return row.to_dict()


def mark_result(action_id: str, status: str, aws_request_id: str | None, message: str | None) -> None:
    """REQUESTED 이력을 결과 상태로 바꾼다."""
    with get_sessionmaker()() as session:
        session.execute(
            update(ActionLog)
            .where(ActionLog.id == action_id, ActionLog.status == STATUS_REQUESTED)
            .values(status=status, aws_request_id=aws_request_id, message=message[:500] if message else None)
        )
        session.commit()
