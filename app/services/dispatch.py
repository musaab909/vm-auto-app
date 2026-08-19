"""Transactional job dispatch.

A job row and its queue message must not race. If the message is published before
the transaction commits, a worker can dequeue it and find no row — which is
exactly what happens with FastAPI `BackgroundTasks`, since those run before the
request's session-scoped dependency commits.

So enqueueing is deferred to SQLAlchemy's `after_commit` event: the task is
published only once the row is durably visible, and a rollback discards the
pending publish instead of queueing work for a job that never existed.

This is the lightweight form of a transactional outbox. It does not survive a
process crash in the window between commit and publish — `requeue_stuck_jobs`
and the reconciler are what recover those.
"""

from __future__ import annotations

import structlog
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db import SessionLocal

log = structlog.get_logger(__name__)

_PENDING_KEY = "pending_job_ids"


def enqueue_after_commit(session: Session, job_id: str) -> None:
    """Publish `job_id` to the queue once the current transaction commits."""
    session.info.setdefault(_PENDING_KEY, []).append(job_id)


def _drain(session: Session) -> None:
    job_ids = session.info.pop(_PENDING_KEY, [])
    if not job_ids:
        return

    # Imported here rather than at module scope: app.workers.tasks pulls in the
    # driver and billing stacks, and importing that from app.db's import chain
    # would be circular.
    from app.workers.tasks import enqueue_job

    for job_id in job_ids:
        try:
            enqueue_job(job_id)
        except Exception:
            # The commit already succeeded, so raising here would report failure
            # for work that was actually persisted. The job stays QUEUED and
            # `requeue_stuck_jobs` picks it up.
            log.exception("dispatch.enqueue_failed", job_id=job_id)


def _discard(session: Session) -> None:
    """Drop pending publishes when the transaction that created them rolled back."""
    session.info.pop(_PENDING_KEY, None)


# Registered once against the session factory, so no per-request listener is
# attached and none can leak.
event.listen(SessionLocal, "after_commit", _drain)
event.listen(SessionLocal, "after_rollback", _discard)
event.listen(SessionLocal, "after_soft_rollback", lambda s, t: _discard(s))
