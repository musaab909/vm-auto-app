"""Instance lifecycle routes.

Every mutating route follows the same shape: validate and write state in the
request transaction, then enqueue the Celery task *after* commit. Enqueuing
before commit would let a worker pick up a job row that is not yet visible.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import (
    ActiveAccount,
    CurrentAccount,
    CurrentUser,
    DbSession,
    OwnedInstance,
    client_ip,
)
from app.billing import subscriptions as subscription_service
from app.drivers import get_driver
from app.models.billing import Subscription
from app.models.catalog import Plan
from app.models.compute import Instance, Job
from app.models.enums import InstanceStatus, JobType, SubscriptionStatus
from app.schemas import (
    ConsoleOut,
    ConsoleSessionOut,
    InstanceActionRequest,
    InstanceCreate,
    InstanceCreated,
    InstanceOut,
    JobOut,
    RebuildRequest,
    ResizeRequest,
)
from app.services import audit, provisioning
from app.services import console as console_service
from app.services.dispatch import enqueue_after_commit
from app.services.errors import (
    NotFoundError,
    ValidationError,
)

router = APIRouter(prefix="/instances", tags=["instances"])


@router.get("", response_model=list[InstanceOut])
def list_instances(session: DbSession, account: CurrentAccount):
    return (
        session.execute(
            select(Instance)
            .where(
                Instance.account_id == account.id,
                Instance.status != InstanceStatus.DELETED,
            )
            .order_by(Instance.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post("", response_model=InstanceCreated, status_code=status.HTTP_201_CREATED)
def create_instance(
    payload: InstanceCreate,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: ActiveAccount,
):
    instance, job, root_password = provisioning.order_instance(
        session,
        account=account,
        plan_slug=payload.plan,
        image_slug=payload.image,
        region_slug=payload.region,
        label=payload.label,
        hostname=payload.hostname,
        ssh_key_ids=payload.ssh_key_ids,
        actor_user_id=user.id,
    )

    plan = session.get(Plan, instance.plan_id)
    if plan is not None:
        subscription_service.create_subscription(
            session, account_id=account.id, plan=plan, instance=instance
        )

    # Published only once this transaction commits, so a worker can never dequeue
    # the task before the job row is visible.
    enqueue_after_commit(session, job.id)

    return InstanceCreated(
        instance=InstanceOut.model_validate(instance),
        job_id=job.id,
        # The only time the plaintext exists; the database keeps a hash.
        root_password=root_password,
    )


@router.get("/{instance_id}", response_model=InstanceOut)
def get_instance(instance: OwnedInstance):
    return instance


@router.get("/{instance_id}/jobs", response_model=list[JobOut])
def list_jobs(instance: OwnedInstance, session: DbSession, limit: int = 20):
    return (
        session.execute(
            select(Job)
            .where(Job.instance_id == instance.id)
            .order_by(Job.created_at.desc())
            .limit(min(limit, 100))
        )
        .scalars()
        .all()
    )


def _queue(
    session,
    instance: Instance,
    job_type: JobType,
    user_id: str,
    payload: dict | None = None,
) -> Job:
    job = provisioning.request_action(
        session,
        instance=instance,
        job_type=job_type,
        payload=payload,
        actor_user_id=user_id,
    )
    enqueue_after_commit(session, job.id)
    return job


@router.post("/{instance_id}/start", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def start_instance(instance: OwnedInstance, session: DbSession, user: CurrentUser):
    return _queue(session, instance, JobType.START, user.id)


@router.post("/{instance_id}/stop", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def stop_instance(
    payload: InstanceActionRequest,
    instance: OwnedInstance,
    session: DbSession,
    user: CurrentUser,
):
    return _queue(session, instance, JobType.STOP, user.id, {"hard": payload.hard})


@router.post("/{instance_id}/reboot", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def reboot_instance(
    payload: InstanceActionRequest,
    instance: OwnedInstance,
    session: DbSession,
    user: CurrentUser,
):
    return _queue(session, instance, JobType.REBOOT, user.id, {"hard": payload.hard})


@router.post("/{instance_id}/resize", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def resize_instance(
    payload: ResizeRequest,
    instance: OwnedInstance,
    session: DbSession,
    user: CurrentUser,
):
    plan = session.execute(
        select(Plan).where(Plan.slug == payload.plan, Plan.is_active.is_(True))
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Plan '{payload.plan}' is not available")

    # vSphere can grow a disk online but never shrink one, and a smaller plan
    # would leave the guest filesystem larger than its backing disk.
    if plan.disk_gb < instance.disk_gb:
        raise ValidationError(
            f"Cannot resize down: {plan.name} offers {plan.disk_gb}GB but this "
            f"server already uses {instance.disk_gb}GB. Disks cannot be shrunk."
        )

    job = _queue(session, instance, JobType.RESIZE, user.id, {"plan_id": plan.id})

    # Reprice the subscription in the same transaction, so the customer is never
    # billed the old rate for new hardware.
    subscription = session.execute(
        select(Subscription).where(
            Subscription.instance_id == instance.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
        )
    ).scalar_one_or_none()
    if subscription is not None:
        proration = subscription_service.change_plan(session, subscription, plan)
        audit.record(
            session,
            action="subscription.plan_changed",
            actor_user_id=user.id,
            account_id=instance.account_id,
            target_type="subscription",
            target_id=subscription.id,
            credit_cents=proration["credit_cents"],
            charge_cents=proration["charge_cents"],
            net_cents=proration["net_cents"],
        )

    return job


@router.post("/{instance_id}/rebuild", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def rebuild_instance(
    payload: RebuildRequest,
    instance: OwnedInstance,
    session: DbSession,
    user: CurrentUser,
):
    if not payload.confirm:
        raise ValidationError(
            "Rebuilding erases every disk on this server. Set 'confirm' to true to proceed."
        )

    job_payload: dict = {"ssh_key_ids": payload.ssh_key_ids}
    if payload.image:
        from app.models.catalog import OsImage

        image = session.execute(
            select(OsImage).where(OsImage.slug == payload.image, OsImage.is_active.is_(True))
        ).scalar_one_or_none()
        if image is None:
            raise NotFoundError(f"Image '{payload.image}' is not available")
        if image.min_disk_gb > instance.disk_gb:
            raise ValidationError(f"{image.name} needs at least {image.min_disk_gb}GB of disk")
        job_payload["image_id"] = image.id

    return _queue(session, instance, JobType.REBUILD, user.id, job_payload)


@router.delete("/{instance_id}", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def delete_instance(
    instance: OwnedInstance,
    session: DbSession,
    user: CurrentUser,
):
    job = _queue(session, instance, JobType.DELETE, user.id)

    # Cancel billing immediately: a deleted server must stop generating invoices
    # even if the hypervisor delete is still in flight.
    subscription = session.execute(
        select(Subscription).where(
            Subscription.instance_id == instance.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
        )
    ).scalar_one_or_none()
    if subscription is not None:
        subscription_service.cancel(session, subscription, immediate=True)

    return job


@router.post("/{instance_id}/console", response_model=ConsoleOut)
def open_console(instance: OwnedInstance, request: Request, session: DbSession, user: CurrentUser):
    """Mint a single-use console handle.

    The WebMKS ticket itself is deliberately not returned: it is a bearer
    credential for the VM's screen, and a JSON body ends up in logs and proxy
    caches. The handle is exchanged once at `GET /console/{handle}`.
    """
    handle, record = console_service.mint(
        session,
        instance=instance,
        user_id=user.id,
        driver=get_driver(),
        source_ip=client_ip(request),
    )
    return ConsoleOut(
        handle=handle,
        redeem_url=f"/api/v1/instances/console/{handle}",
        expires_at=record.expires_at,
    )


@router.get("/console/{handle}", response_model=ConsoleSessionOut)
def redeem_console(handle: str, session: DbSession, user: CurrentUser):
    """Exchange a handle for the connection details. Works exactly once."""
    details = console_service.redeem(session, handle, user_id=user.id)
    return ConsoleSessionOut(
        instance_id=details.instance_id,
        host=details.host,
        port=details.port,
        url=details.url,
        mode=details.mode,
        viewer_url=details.viewer_url,
    )
