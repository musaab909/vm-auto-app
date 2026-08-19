"""Celery application and beat schedule."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "hostplane",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Redelivered if a worker dies mid-task. Provisioning is idempotent (drivers
    # adopt an existing VM rather than cloning a second one), so at-least-once
    # delivery is the safer trade than losing a customer's order.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600,
    # Lets tests run tasks inline with no broker.
    task_always_eager=settings.task_always_eager,
    task_eager_propagates=settings.task_always_eager,
    task_routes={
        "hostplane.provisioning.*": {"queue": "provisioning"},
        "hostplane.billing.*": {"queue": "billing"},
        "hostplane.maintenance.*": {"queue": "maintenance"},
    },
    beat_schedule={
        "renew-due-subscriptions": {
            "task": "hostplane.billing.run_renewals",
            "schedule": crontab(hour=2, minute=0),
        },
        "process-dunning": {
            "task": "hostplane.billing.run_dunning",
            "schedule": crontab(hour=3, minute=0),
        },
        "reconcile-instances": {
            "task": "hostplane.maintenance.reconcile",
            "schedule": crontab(minute="*/15"),
        },
        "sweep-ip-quarantine": {
            "task": "hostplane.maintenance.sweep_quarantine",
            "schedule": crontab(hour="*/1", minute=5),
        },
        "purge-console-tickets": {
            "task": "hostplane.maintenance.purge_console_tickets",
            "schedule": crontab(hour=4, minute=30),
        },
        "requeue-stuck-jobs": {
            "task": "hostplane.maintenance.requeue_stuck_jobs",
            "schedule": crontab(minute="*/10"),
        },
    },
)
