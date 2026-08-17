"""Celery tasks.

Every hypervisor call in the platform happens here, never in a web request.

Retry policy: `DriverError` carries a `permanent` flag. Permanent failures (a bad
spec, a missing template) fail immediately rather than burning three attempts on
an error that cannot change; everything else backs off exponentially.
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from celery import Task
from sqlalchemy import select

from app.billing import dunning as dunning_service
from app.billing import get_gateway
from app.billing import subscriptions as subscription_service
from app.db import session_scope
from app.drivers import get_driver
from app.models.compute import Instance, Job
from app.models.enums import JobStatus, JobType
from app.security import utcnow
from app.services import ipam, provisioning
from app.services.errors import DriverError
from app.workers.celery_app import celery_app

log = structlog.get_logger(__name__)

# Base backoff; attempt N waits BACKOFF_BASE * 2**(N-1) seconds.
BACKOFF_BASE_SECONDS = 30
STUCK_JOB_MINUTES = 30


@celery_app.task(name="hostplane.provisioning.run_job", bind=True, max_retries=3)
def run_job(self: Task, job_id: str) -> dict:
    """Execute one lifecycle job.

    Failure bookkeeping is committed in its own transaction, separate from the
    work: rolling back the attempt must not also roll back the record that the
    attempt happened, or a failing job would look untouched forever.
    """
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"job_id": job_id, "status": "missing"}
        if job.status is JobStatus.SUCCEEDED:
            return {"job_id": job_id, "status": "already_succeeded"}

        job.celery_task_id = self.request.id
        job_type = job.type
        attempts = job.attempts
        max_attempts = job.max_attempts

    try:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return {"job_id": job_id, "status": "missing"}
            provisioning.run_job(session, job, get_driver())
            result = dict(job.result)
        log.info("job.succeeded", job_id=job_id, type=str(job_type))
        return {"job_id": job_id, "status": "succeeded", "result": result}

    except DriverError as exc:
        final = exc.permanent or (attempts + 1) >= max_attempts
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is not None:
                provisioning.fail_job(session, job, exc.message, final=final)

        log.warning(
            "job.failed",
            job_id=job_id,
            type=str(job_type),
            permanent=exc.permanent,
            final=final,
            error=exc.message,
        )
        if final:
            return {"job_id": job_id, "status": "failed", "error": exc.message}
        raise self.retry(exc=exc, countdown=BACKOFF_BASE_SECONDS * (2**attempts)) from exc

    except Exception as exc:
        final = (attempts + 1) >= max_attempts
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is not None:
                provisioning.fail_job(session, job, str(exc), final=final)
        log.exception("job.errored", job_id=job_id, type=str(job_type))
        if final:
            return {"job_id": job_id, "status": "failed", "error": str(exc)}
        raise self.retry(exc=exc, countdown=BACKOFF_BASE_SECONDS * (2**attempts)) from exc


def enqueue_job(job_id: str) -> None:
    """Queue a job for execution.

    Call this only after the transaction that created the job has committed — a
    worker can pick the task up before the caller's commit lands otherwise, and
    then the row it needs does not exist yet.
    """
    run_job.delay(job_id)


@celery_app.task(name="hostplane.billing.run_renewals")
def run_renewals() -> dict:
    """Invoice and charge every subscription whose period has ended."""
    gateway = get_gateway()
    renewed = failed = 0

    with session_scope() as session:
        due = subscription_service.due_for_renewal(session)
        for subscription in due:
            outcome = dunning_service.renew_subscription(session, subscription, gateway)
            if outcome.paid:
                renewed += 1
            else:
                failed += 1

    _drain_system_jobs()
    log.info("billing.renewals", renewed=renewed, failed=failed)
    return {"renewed": renewed, "failed": failed}


@celery_app.task(name="hostplane.billing.run_dunning")
def run_dunning() -> dict:
    """Run every retry, suspension and termination that has come due."""
    gateway = get_gateway()
    with session_scope() as session:
        outcomes = dunning_service.process_due_dunning(session, gateway)
        summary = {"processed": len(outcomes)}

    _drain_system_jobs()
    log.info("billing.dunning", **summary)
    return summary


def _drain_system_jobs() -> None:
    """Queue lifecycle jobs that billing created (suspend, resume, delete).

    Billing writes job rows rather than calling Celery directly, so those rows
    only become tasks once its transaction has committed.
    """
    with session_scope() as session:
        job_ids = [
            row[0]
            for row in session.execute(
                select(Job.id).where(
                    Job.status == JobStatus.QUEUED,
                    Job.celery_task_id.is_(None),
                    Job.type.in_([JobType.SUSPEND, JobType.RESUME, JobType.DELETE]),
                )
            ).all()
        ]
    for job_id in job_ids:
        enqueue_job(job_id)


@celery_app.task(name="hostplane.maintenance.reconcile")
def reconcile(max_instances: int = 200) -> dict:
    """Re-sync recorded instance state against the hypervisor."""
    driver = get_driver()
    checked = changed = 0

    with session_scope() as session:
        stale = provisioning.find_stale_instances(session, utcnow() - timedelta(minutes=15))
        for instance in stale[:max_instances]:
            checked += 1
            try:
                result = provisioning.reconcile_instance(session, instance, driver)
            except DriverError as exc:
                log.warning("reconcile.failed", instance_id=instance.id, error=exc.message)
                continue
            if result.get("changed"):
                changed += 1
                log.info("reconcile.drift", **result)

    return {"checked": checked, "changed": changed}


@celery_app.task(name="hostplane.maintenance.sweep_quarantine")
def sweep_quarantine() -> dict:
    with session_scope() as session:
        released = ipam.sweep_quarantine(session)
    return {"released": released}


@celery_app.task(name="hostplane.maintenance.requeue_stuck_jobs")
def requeue_stuck_jobs() -> dict:
    """Recover jobs orphaned by a worker that died mid-flight.

    `task_acks_late` covers a clean crash, but a job whose worker was killed
    between marking RUNNING and finishing would otherwise sit there forever with
    the instance stuck in a transitional state.
    """
    cutoff = utcnow() - timedelta(minutes=STUCK_JOB_MINUTES)
    requeued: list[str] = []

    with session_scope() as session:
        stuck = (
            session.execute(
                select(Job).where(
                    Job.status == JobStatus.RUNNING,
                    Job.started_at.is_not(None),
                    Job.started_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for job in stuck:
            if job.attempts >= job.max_attempts:
                provisioning.fail_job(
                    session, job, "Job exceeded its attempt limit while running", final=True
                )
                continue
            job.status = JobStatus.QUEUED
            job.celery_task_id = None
            requeued.append(job.id)

    for job_id in requeued:
        enqueue_job(job_id)

    if requeued:
        log.warning("jobs.requeued", count=len(requeued))
    return {"requeued": len(requeued)}


@celery_app.task(name="hostplane.maintenance.sync_instance")
def sync_instance(instance_id: str) -> dict:
    """Reconcile one instance on demand."""
    with session_scope() as session:
        instance = session.get(Instance, instance_id)
        if instance is None:
            return {"instance_id": instance_id, "status": "missing"}
        return provisioning.reconcile_instance(session, instance, get_driver())
