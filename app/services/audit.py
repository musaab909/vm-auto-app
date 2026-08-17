"""Audit logging."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.identity import AuditLog
from app.security import utcnow


def record(
    session: Session,
    *,
    action: str,
    actor_user_id: str | None = None,
    account_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    **details: object,
) -> AuditLog:
    """Append one audit entry. The caller's transaction owns the commit."""
    entry = AuditLog(
        created_at=utcnow(),
        actor_user_id=actor_user_id,
        account_id=account_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        source_ip=source_ip,
        user_agent=(user_agent or "")[:300] or None,
        details={k: v for k, v in details.items() if v is not None},
    )
    session.add(entry)
    return entry
