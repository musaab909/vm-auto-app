"""Instance lifecycle orchestration.

The split that matters: functions named `order_*` / `request_*` run inside a web
request and only touch the database, while `execute_*` functions run in a Celery
worker and are the only code that talks to a hypervisor. A wedged vCenter can
therefore never block an HTTP response — the failure mode the prototype had,
where a single provisioning call held the request thread for minutes.
"""

from __future__ import annotations

import re
from datetime import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.drivers import get_driver
from app.drivers.base import HypervisorDriver, InstanceSpec, NetworkSpec, PowerState
from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.compute import Instance, Job
from app.models.enums import InstanceStatus, IPStatus, JobStatus, JobType
from app.models.identity import Account, SshKey
from app.models.network import IpAddress, IpPool
from app.security import generate_password, hash_password, utcnow
from app.services import audit, ipam, quota
from app.services.errors import (
    DriverError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

log = structlog.get_logger(__name__)

HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Which statuses each action may legally start from. Centralised so the API, the
# portal and the workers cannot disagree about what is allowed.
ALLOWED_TRANSITIONS: dict[JobType, set[InstanceStatus]] = {
    JobType.START: {InstanceStatus.STOPPED, InstanceStatus.ERROR},
    JobType.STOP: {InstanceStatus.RUNNING, InstanceStatus.ERROR},
    JobType.REBOOT: {InstanceStatus.RUNNING},
    JobType.REBUILD: {InstanceStatus.RUNNING, InstanceStatus.STOPPED, InstanceStatus.ERROR},
    JobType.RESIZE: {InstanceStatus.STOPPED},
    JobType.DELETE: {
        InstanceStatus.RUNNING,
        InstanceStatus.STOPPED,
        InstanceStatus.SUSPENDED,
        InstanceStatus.ERROR,
        InstanceStatus.PENDING,
    },
    JobType.SUSPEND: {InstanceStatus.RUNNING, InstanceStatus.STOPPED},
    JobType.RESUME: {InstanceStatus.SUSPENDED},
}

# Status to move to while a job runs, so the UI reflects work in flight.
IN_FLIGHT_STATUS: dict[JobType, InstanceStatus] = {
    JobType.PROVISION: InstanceStatus.PROVISIONING,
    JobType.START: InstanceStatus.STARTING,
    JobType.STOP: InstanceStatus.STOPPING,
    JobType.REBOOT: InstanceStatus.REBOOTING,
    JobType.REBUILD: InstanceStatus.REBUILDING,
    JobType.RESIZE: InstanceStatus.RESIZING,
    JobType.DELETE: InstanceStatus.DELETING,
}


def validate_hostname(hostname: str) -> str:
    label = hostname.strip().lower()
    if not label:
        raise ValidationError("Hostname is required")
    if len(label) > 63:
        raise ValidationError("Hostname must be 63 characters or fewer")
    if not HOSTNAME_RE.match(label):
        raise ValidationError(
            "Hostname may contain only lowercase letters, digits and hyphens, "
            "and must start and end with a letter or digit"
        )
    return label


# --- ordering (request path, database only) ---------------------------------


def order_instance(
    session: Session,
    *,
    account: Account,
    plan_slug: str,
    image_slug: str,
    region_slug: str,
    label: str,
    hostname: str | None = None,
    ssh_key_ids: list[str] | None = None,
    actor_user_id: str | None = None,
) -> tuple[Instance, Job, str]:
    """Create an instance, allocate its IP and queue provisioning.

    Everything here happens in one transaction, so a failure at any point leaves
    no half-created instance and no leaked IP address. Returns the instance, the
    provisioning job, and the generated root password — the only moment the
    plaintext password exists.
    """
    plan = session.execute(
        select(Plan).where(Plan.slug == plan_slug, Plan.is_active.is_(True))
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError(f"Plan '{plan_slug}' is not available")

    image = session.execute(
        select(OsImage).where(OsImage.slug == image_slug, OsImage.is_active.is_(True))
    ).scalar_one_or_none()
    if image is None:
        raise NotFoundError(f"Image '{image_slug}' is not available")

    region = session.execute(
        select(Region).where(Region.slug == region_slug, Region.is_active.is_(True))
    ).scalar_one_or_none()
    if region is None:
        raise NotFoundError(f"Region '{region_slug}' is not available")

    availability = session.execute(
        select(PlanRegion).where(
            PlanRegion.plan_id == plan.id,
            PlanRegion.region_id == region.id,
            PlanRegion.is_available.is_(True),
        )
    ).scalar_one_or_none()
    if availability is None:
        raise ValidationError(f"Plan '{plan.name}' is not offered in region '{region.name}'")

    if image.min_disk_gb > plan.disk_gb:
        raise ValidationError(
            f"{image.name} needs at least {image.min_disk_gb}GB of disk, "
            f"but {plan.name} provides {plan.disk_gb}GB"
        )

    quota.check_can_create_instance(session, account, plan)

    resolved_hostname = validate_hostname(hostname or label)

    instance = Instance(
        account_id=account.id,
        plan_id=plan.id,
        image_id=image.id,
        region_id=region.id,
        label=label.strip()[:100],
        hostname=resolved_hostname,
        status=InstanceStatus.PENDING,
        # Snapshotted from the plan so editing the catalog later cannot silently
        # change what an existing customer is running.
        vcpu=plan.vcpu,
        ram_mb=plan.ram_mb,
        disk_gb=plan.disk_gb,
        default_username=image.default_username,
    )
    session.add(instance)
    session.flush()

    # Allocated in the same transaction as the instance row: if anything below
    # fails, the address is rolled back rather than leaked.
    ipam.allocate_ip(session, region_id=region.id, instance_id=instance.id)

    root_password = generate_password()
    instance.root_password_hash = hash_password(root_password)
    instance.root_password_set_at = utcnow()

    keys = _resolve_ssh_keys(session, account.id, ssh_key_ids)

    job = Job(
        instance_id=instance.id,
        account_id=account.id,
        type=JobType.PROVISION,
        status=JobStatus.QUEUED,
        payload={
            "ssh_key_ids": [k.id for k in keys],
            # Held only for the duration of provisioning; `execute_provision`
            # clears it once the guest has been configured.
            "root_password": root_password,
        },
    )
    session.add(job)
    session.flush()

    audit.record(
        session,
        action="instance.ordered",
        actor_user_id=actor_user_id,
        account_id=account.id,
        target_type="instance",
        target_id=instance.id,
        plan=plan.slug,
        image=image.slug,
        region=region.slug,
    )

    return instance, job, root_password


def _resolve_ssh_keys(
    session: Session, account_id: str, ssh_key_ids: list[str] | None
) -> list[SshKey]:
    if not ssh_key_ids:
        return []
    keys = (
        session.execute(
            select(SshKey).where(SshKey.account_id == account_id, SshKey.id.in_(ssh_key_ids))
        )
        .scalars()
        .all()
    )
    missing = set(ssh_key_ids) - {k.id for k in keys}
    if missing:
        raise NotFoundError(f"SSH key(s) not found: {', '.join(sorted(missing))}")
    return list(keys)


def request_action(
    session: Session,
    *,
    instance: Instance,
    job_type: JobType,
    payload: dict | None = None,
    actor_user_id: str | None = None,
) -> Job:
    """Validate a lifecycle action against current state and queue it."""
    allowed = ALLOWED_TRANSITIONS.get(job_type)
    if allowed is None:
        raise ValidationError(f"Unsupported action '{job_type}'")

    if instance.status not in allowed:
        raise InvalidStateError(
            f"Cannot {job_type} an instance that is {instance.status}",
            details={"status": str(instance.status), "action": str(job_type)},
        )

    # One in-flight job per instance: concurrent power actions would race in the
    # hypervisor and leave the recorded status wrong.
    in_flight = session.execute(
        select(Job).where(
            Job.instance_id == instance.id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    ).scalar_one_or_none()
    if in_flight is not None:
        raise InvalidStateError(
            f"Another operation ({in_flight.type}) is already running on this instance",
            details={"job_id": in_flight.id},
        )

    job = Job(
        instance_id=instance.id,
        account_id=instance.account_id,
        type=job_type,
        status=JobStatus.QUEUED,
        payload=payload or {},
    )
    session.add(job)

    next_status = IN_FLIGHT_STATUS.get(job_type)
    if next_status is not None:
        instance.status = next_status

    session.flush()

    audit.record(
        session,
        action=f"instance.{job_type}",
        actor_user_id=actor_user_id,
        account_id=instance.account_id,
        target_type="instance",
        target_id=instance.id,
        job_id=job.id,
    )
    return job


# --- execution (worker path, talks to the hypervisor) -----------------------


def build_spec(
    session: Session,
    instance: Instance,
    *,
    root_password: str | None = None,
    ssh_key_ids: list[str] | None = None,
) -> InstanceSpec:
    """Assemble a fully-resolved driver spec from database state."""
    address = session.execute(
        select(IpAddress).where(
            IpAddress.instance_id == instance.id,
            IpAddress.status == IPStatus.ASSIGNED,
            IpAddress.is_primary.is_(True),
        )
    ).scalar_one_or_none()
    if address is None:
        raise DriverError(f"Instance {instance.id} has no assigned IP address", permanent=True)

    pool = session.get(IpPool, address.pool_id)
    if pool is None:
        raise DriverError(f"IP pool {address.pool_id} is missing", permanent=True)

    region = session.get(Region, instance.region_id)
    image = session.get(OsImage, instance.image_id)
    if region is None or image is None:
        raise DriverError("Instance references a missing region or image", permanent=True)

    keys = _resolve_ssh_keys(session, instance.account_id, ssh_key_ids)

    return InstanceSpec(
        name=f"{instance.hostname}-{instance.id[:8]}",
        hostname=instance.hostname,
        vcpu=instance.vcpu,
        ram_mb=instance.ram_mb,
        disk_gb=instance.disk_gb,
        template=image.vsphere_template or image.slug,
        network=NetworkSpec(
            ip_address=address.address,
            prefix_length=pool.prefix_length,
            gateway=pool.gateway,
            dns_servers=pool.dns_list,
            network_name=pool.vsphere_network or region.vsphere_network,
            vlan_id=pool.vlan_id,
        ),
        datacenter=region.vsphere_datacenter,
        cluster=region.vsphere_cluster,
        resource_pool=region.vsphere_resource_pool,
        datastore=region.vsphere_datastore,
        folder=region.vsphere_folder,
        root_password=root_password,
        ssh_keys=[k.public_key for k in keys],
        default_username=instance.default_username,
        instance_id=instance.id,
        account_id=instance.account_id,
    )


def clone_slots_in_use(
    session: Session, region_id: str, *, exclude_job_id: str | None = None
) -> int:
    """Provision jobs currently cloning into `region_id`.

    Counts RUNNING provision jobs rather than tracking a separate counter: the
    job rows are already the source of truth, and a counter would drift every
    time a worker died between decrement and commit.
    """
    stmt = (
        select(func.count())
        .select_from(Job)
        .join(Instance, Job.instance_id == Instance.id)
        .where(
            Job.type == JobType.PROVISION,
            Job.status == JobStatus.RUNNING,
            Instance.region_id == region_id,
        )
    )
    if exclude_job_id is not None:
        stmt = stmt.where(Job.id != exclude_job_id)
    return int(session.execute(stmt).scalar_one())


def acquire_clone_slot(session: Session, job: Job, instance: Instance) -> None:
    """Refuse to start a clone when the region is already at its limit.

    Raises a *retryable* `DriverError`, so the deferral rides the worker's
    existing exponential backoff instead of needing its own scheduling. The
    region row is locked first so two workers cannot both read "one slot left".
    """
    region = session.execute(
        select(Region).where(Region.id == instance.region_id).with_for_update()
    ).scalar_one_or_none()
    if region is None:
        raise DriverError("Instance references a missing region", permanent=True)

    limit = region.max_concurrent_clones
    if limit <= 0:
        return  # 0 means "no limit", for operators who throttle elsewhere

    # This job is already RUNNING by the time the executor is reached, so it must
    # not count against its own limit.
    in_use = clone_slots_in_use(session, region.id, exclude_job_id=job.id)
    if in_use >= limit:
        log.info(
            "provisioning.clone_deferred",
            region=region.slug,
            in_use=in_use,
            limit=limit,
            job_id=job.id,
        )
        raise DriverError(
            f"Region '{region.slug}' is at its clone limit ({in_use}/{limit}); "
            "this order will start automatically when a slot frees up"
        )


def execute_provision(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)

    # Checked before any hypervisor work, so a deferred order costs one query
    # rather than a rejected clone task.
    acquire_clone_slot(session, job, instance)

    spec = build_spec(
        session,
        instance,
        root_password=job.payload.get("root_password"),
        ssh_key_ids=job.payload.get("ssh_key_ids"),
    )

    result = driver.create_instance(spec)

    instance.hypervisor_ref = result.ref
    instance.hypervisor_uuid = result.uuid
    instance.status = (
        InstanceStatus.RUNNING if result.power_state is PowerState.ON else InstanceStatus.STOPPED
    )
    instance.provisioned_at = utcnow()
    instance.last_synced_at = utcnow()
    instance.error_message = None

    # The plaintext password has served its purpose; keeping it in a JSONB column
    # would leave credentials sitting in the database indefinitely.
    if "root_password" in job.payload:
        payload = dict(job.payload)
        payload.pop("root_password")
        payload["root_password_cleared"] = True
        job.payload = payload

    job.result = {"ref": result.ref, "uuid": result.uuid, "ip": instance.primary_ip}


def execute_start(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)
    driver.power_on(_require_ref(instance))
    instance.status = InstanceStatus.RUNNING
    instance.last_synced_at = utcnow()


def execute_stop(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)
    driver.power_off(_require_ref(instance), hard=bool(job.payload.get("hard")))
    instance.status = InstanceStatus.STOPPED
    instance.last_synced_at = utcnow()


def execute_reboot(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)
    driver.reboot(_require_ref(instance), hard=bool(job.payload.get("hard")))
    instance.status = InstanceStatus.RUNNING
    instance.last_synced_at = utcnow()


def execute_resize(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)

    new_plan_id = job.payload.get("plan_id")
    plan = session.get(Plan, new_plan_id) if new_plan_id else None
    if plan is None:
        raise DriverError("Resize job is missing a valid target plan", permanent=True)

    driver.resize(_require_ref(instance), vcpu=plan.vcpu, ram_mb=plan.ram_mb, disk_gb=plan.disk_gb)
    instance.plan_id = plan.id
    instance.vcpu, instance.ram_mb, instance.disk_gb = plan.vcpu, plan.ram_mb, plan.disk_gb
    instance.status = InstanceStatus.STOPPED
    instance.last_synced_at = utcnow()


def execute_rebuild(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)

    image_id = job.payload.get("image_id")
    if image_id:
        image = session.get(OsImage, image_id)
        if image is None:
            raise DriverError("Rebuild job references a missing image", permanent=True)
        instance.image_id = image.id
        instance.default_username = image.default_username

    root_password = job.payload.get("root_password") or generate_password()
    spec = build_spec(
        session,
        instance,
        root_password=root_password,
        ssh_key_ids=job.payload.get("ssh_key_ids"),
    )

    result = driver.rebuild(_require_ref(instance), spec)

    instance.hypervisor_ref = result.ref
    instance.hypervisor_uuid = result.uuid
    instance.root_password_hash = hash_password(root_password)
    instance.root_password_set_at = utcnow()
    instance.status = InstanceStatus.RUNNING
    instance.last_synced_at = utcnow()

    payload = dict(job.payload)
    payload.pop("root_password", None)
    job.payload = payload
    job.result = {"ref": result.ref, "rebuilt": True}


def execute_delete(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)

    if instance.hypervisor_ref:
        driver.delete_instance(instance.hypervisor_ref)

    # Released only after the hypervisor confirms the VM is gone, so an address is
    # never handed to a new customer while the old VM can still emit traffic.
    freed = ipam.release_instance_ips(session, instance.id)

    instance.status = InstanceStatus.DELETED
    instance.deleted_at = utcnow()
    instance.hypervisor_ref = None
    job.result = {"deleted": True, "ips_released": freed}


def execute_suspend(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    """Power off for non-payment while preserving the disk."""
    driver = driver or get_driver()
    instance = _job_instance(session, job)
    if instance.hypervisor_ref:
        driver.power_off(instance.hypervisor_ref, hard=False)
    instance.status = InstanceStatus.SUSPENDED
    instance.suspended_at = utcnow()


def execute_resume(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    driver = driver or get_driver()
    instance = _job_instance(session, job)
    if instance.hypervisor_ref:
        driver.power_on(instance.hypervisor_ref)
    instance.status = InstanceStatus.RUNNING
    instance.suspended_at = None


EXECUTORS = {
    JobType.PROVISION: execute_provision,
    JobType.START: execute_start,
    JobType.STOP: execute_stop,
    JobType.REBOOT: execute_reboot,
    JobType.RESIZE: execute_resize,
    JobType.REBUILD: execute_rebuild,
    JobType.DELETE: execute_delete,
    JobType.SUSPEND: execute_suspend,
    JobType.RESUME: execute_resume,
}


def run_job(session: Session, job: Job, driver: HypervisorDriver | None = None) -> None:
    """Dispatch a job to its executor and record the outcome.

    Marking the job failed and the instance errored is deliberately not the same
    thing as rolling back: the caller commits this state so the customer can see
    what went wrong instead of the failure vanishing.
    """
    executor = EXECUTORS.get(job.type)
    if executor is None:
        raise DriverError(f"No executor registered for job type '{job.type}'", permanent=True)

    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.started_at = job.started_at or utcnow()
    session.flush()

    executor(session, job, driver)

    job.status = JobStatus.SUCCEEDED
    job.progress = 100
    job.finished_at = utcnow()
    job.error = None
    session.flush()


def fail_job(session: Session, job: Job, error: str, *, final: bool) -> None:
    job.error = error[:4000]
    if final:
        job.status = JobStatus.FAILED
        job.finished_at = utcnow()
        instance = session.get(Instance, job.instance_id) if job.instance_id else None
        if instance is not None:
            instance.status = InstanceStatus.ERROR
            instance.error_message = error[:2000]
            # A provisioning attempt that never produced a VM is holding an address
            # for nothing; give it back rather than leaking pool capacity.
            if job.type == JobType.PROVISION and not instance.hypervisor_ref:
                ipam.release_instance_ips(session, instance.id, quarantine=False)
    session.flush()


def _job_instance(session: Session, job: Job) -> Instance:
    if job.instance_id is None:
        raise DriverError("Job is not bound to an instance", permanent=True)
    instance = session.get(Instance, job.instance_id)
    if instance is None:
        raise DriverError(f"Instance {job.instance_id} no longer exists", permanent=True)
    return instance


def _require_ref(instance: Instance) -> str:
    if not instance.hypervisor_ref:
        raise DriverError(
            f"Instance {instance.id} has no hypervisor reference; it may have failed to provision",
            permanent=True,
        )
    return instance.hypervisor_ref


# --- reconciliation ---------------------------------------------------------


def reconcile_instance(
    session: Session, instance: Instance, driver: HypervisorDriver | None = None
) -> dict[str, object]:
    """Bring one instance's recorded state back in line with the hypervisor.

    The control plane's row is authoritative for *intent*; the hypervisor is
    authoritative for *fact*. Where they disagree, fact wins and the divergence
    is flagged.
    """
    driver = driver or get_driver()
    now = utcnow()
    changes: dict[str, object] = {"instance_id": instance.id, "changed": False}

    # Recover a VM that was created just before the worker died, before it could
    # be recorded — otherwise it runs forever, unbilled and unmanaged.
    if not instance.hypervisor_ref:
        found = driver.find_by_instance_id(instance.id)
        if found is not None:
            instance.hypervisor_ref = found.ref
            instance.hypervisor_uuid = found.uuid
            changes.update(changed=True, adopted_ref=found.ref)
        else:
            return changes

    remote = driver.get_instance(instance.hypervisor_ref)
    instance.last_synced_at = now

    if not remote.exists:
        if instance.status is not InstanceStatus.DELETED:
            instance.status = InstanceStatus.ERROR
            instance.error_message = "VM is missing from the hypervisor"
            instance.drift_detected_at = now
            changes.update(changed=True, reason="vm_missing")
        return changes

    expected = {
        PowerState.ON: InstanceStatus.RUNNING,
        PowerState.OFF: InstanceStatus.STOPPED,
        PowerState.SUSPENDED: InstanceStatus.SUSPENDED,
    }.get(remote.power_state)

    # Transitional states are mid-flight by definition; correcting them would race
    # the worker that is currently driving the change.
    if expected is None or instance.status.is_transitional:
        return changes

    # A suspended instance is intentionally powered off; that is not drift.
    if instance.status is InstanceStatus.SUSPENDED and expected is InstanceStatus.STOPPED:
        return changes

    if instance.status != expected:
        changes.update(changed=True, from_status=str(instance.status), to_status=str(expected))
        instance.status = expected
        instance.drift_detected_at = now

    return changes


def find_stale_instances(session: Session, older_than: datetime) -> list[Instance]:
    """Instances that have not been synced recently and are worth reconciling."""
    return list(
        session.execute(
            select(Instance).where(
                Instance.status.not_in([InstanceStatus.DELETED, InstanceStatus.PENDING]),
                (Instance.last_synced_at.is_(None)) | (Instance.last_synced_at < older_than),
            )
        )
        .scalars()
        .all()
    )
