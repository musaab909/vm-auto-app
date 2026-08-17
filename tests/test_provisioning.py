"""Instance lifecycle tests against the mock driver."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.drivers.base import PowerState
from app.drivers.mock import MockDriver
from app.models.enums import InstanceStatus, JobStatus, JobType
from app.models.network import IpAddress
from app.security import utcnow, verify_password
from app.services import ipam, provisioning
from app.services.errors import (
    DriverError,
    InvalidStateError,
    NoCapacityError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)


def order(db, account, **overrides):
    kwargs = {
        "plan_slug": "vps-s",
        "image_slug": "ubuntu-24-04",
        "region_slug": "eu-central",
        "label": "web-01",
    }
    kwargs.update(overrides)
    return provisioning.order_instance(db, account=account, **kwargs)


# --- ordering ---------------------------------------------------------------


def test_order_allocates_ip_and_queues_provision(db, catalog, account):
    instance, job, password = order(db, account)

    assert instance.status is InstanceStatus.PENDING
    assert instance.primary_ip is not None
    assert job.type is JobType.PROVISION
    assert job.status is JobStatus.QUEUED

    # Spec is snapshotted from the plan, not referenced live.
    assert (instance.vcpu, instance.ram_mb, instance.disk_gb) == (2, 4096, 50)

    # The plaintext password is returned but only a hash is stored.
    assert len(password) >= 20
    assert instance.root_password_hash is not None
    assert password not in instance.root_password_hash
    assert verify_password(password, instance.root_password_hash)


def test_order_rejects_unknown_plan(db, catalog, account):
    with pytest.raises(NotFoundError, match="Plan"):
        order(db, account, plan_slug="does-not-exist")


def test_order_rejects_plan_not_offered_in_region(db, catalog, account, bigger_plan):
    from app.models.catalog import PlanRegion

    link = db.execute(select(PlanRegion).where(PlanRegion.plan_id == bigger_plan.id)).scalar_one()
    db.delete(link)
    db.flush()

    with pytest.raises(ValidationError, match="not offered in region"):
        order(db, account, plan_slug="vps-m")


def test_order_rejects_image_needing_more_disk(db, catalog, account, image):
    image.min_disk_gb = 500  # larger than the 50GB plan
    db.flush()

    with pytest.raises(ValidationError, match="at least 500GB"):
        order(db, account)


def test_order_enforces_instance_quota(db, catalog, account):
    account.quota.max_instances = 2
    db.flush()

    order(db, account, label="one")
    order(db, account, label="two")

    with pytest.raises(QuotaExceededError, match="instances"):
        order(db, account, label="three")


def test_order_fails_when_no_addresses_remain(db, catalog, account):
    account.quota.max_instances = 50
    db.flush()

    for i in range(5):  # the /29 fixture has exactly 5 usable addresses
        order(db, account, label=f"vm-{i}")

    with pytest.raises(NoCapacityError):
        order(db, account, label="overflow")


def test_failed_order_leaks_no_ip_address(db, catalog, account):
    """A rejected order must not consume pool capacity."""
    db.commit()  # persist the fixtures so the rollback below isolates the order
    before = ipam.region_available_count(db, catalog["region"].id)

    with pytest.raises(NotFoundError):
        order(db, account, image_slug="nonexistent")

    db.rollback()
    assert ipam.region_available_count(db, catalog["region"].id) == before


@pytest.mark.parametrize(
    "hostname,valid",
    [
        ("web-01", True),
        ("a", True),
        ("-leading", False),
        ("trailing-", False),
        ("has_underscore", False),
        ("UPPER", True),  # normalised to lowercase
        ("x" * 64, False),
        ("", False),
    ],
)
def test_hostname_validation(hostname, valid):
    if valid:
        assert provisioning.validate_hostname(hostname) == hostname.lower()
    else:
        with pytest.raises(ValidationError):
            provisioning.validate_hostname(hostname)


# --- execution --------------------------------------------------------------


def test_provision_creates_vm_and_marks_running(db, catalog, account):
    instance, job, _password = order(db, account)
    driver = MockDriver()

    provisioning.run_job(db, job, driver)

    assert job.status is JobStatus.SUCCEEDED
    assert instance.status is InstanceStatus.RUNNING
    assert instance.hypervisor_ref is not None
    assert instance.provisioned_at is not None

    remote = driver.get_instance(instance.hypervisor_ref)
    assert remote.power_state is PowerState.ON
    assert remote.ip_addresses == [instance.primary_ip]


def test_provisioning_clears_plaintext_password_from_job_payload(db, catalog, account):
    """The password must not linger in the jobs table after provisioning."""
    _instance, job, password = order(db, account)
    assert job.payload["root_password"] == password

    provisioning.run_job(db, job, MockDriver())

    assert "root_password" not in job.payload
    assert job.payload.get("root_password_cleared") is True


def test_full_lifecycle_create_stop_start_delete(db, catalog, account):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)
    ref = instance.hypervisor_ref

    stop = provisioning.request_action(db, instance=instance, job_type=JobType.STOP)
    assert instance.status is InstanceStatus.STOPPING
    provisioning.run_job(db, stop, driver)
    assert instance.status is InstanceStatus.STOPPED
    assert driver.get_instance(ref).power_state is PowerState.OFF

    start = provisioning.request_action(db, instance=instance, job_type=JobType.START)
    provisioning.run_job(db, start, driver)
    assert instance.status is InstanceStatus.RUNNING
    assert driver.get_instance(ref).power_state is PowerState.ON

    delete = provisioning.request_action(db, instance=instance, job_type=JobType.DELETE)
    provisioning.run_job(db, delete, driver)

    assert instance.status is InstanceStatus.DELETED
    assert instance.deleted_at is not None
    assert driver.get_instance(ref).exists is False

    # The address is returned to the pool, not leaked.
    freed = (
        db.execute(select(IpAddress).where(IpAddress.instance_id == instance.id)).scalars().all()
    )
    assert freed == []
    assert ipam.region_available_count(db, catalog["region"].id) == 5


def test_delete_releases_ip_back_to_pool(db, catalog, account):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    assert ipam.region_available_count(db, catalog["region"].id) == 4

    delete = provisioning.request_action(db, instance=instance, job_type=JobType.DELETE)
    provisioning.run_job(db, delete, driver)

    assert ipam.region_available_count(db, catalog["region"].id) == 5


def test_resize_applies_new_plan(db, catalog, account, bigger_plan):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    stop = provisioning.request_action(db, instance=instance, job_type=JobType.STOP)
    provisioning.run_job(db, stop, driver)

    resize = provisioning.request_action(
        db, instance=instance, job_type=JobType.RESIZE, payload={"plan_id": bigger_plan.id}
    )
    provisioning.run_job(db, resize, driver)

    assert instance.plan_id == bigger_plan.id
    assert (instance.vcpu, instance.ram_mb, instance.disk_gb) == (4, 8192, 100)


def test_rebuild_regenerates_root_password(db, catalog, account):
    instance, job, original = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)
    original_hash = instance.root_password_hash

    rebuild = provisioning.request_action(db, instance=instance, job_type=JobType.REBUILD)
    provisioning.run_job(db, rebuild, driver)

    assert instance.status is InstanceStatus.RUNNING
    assert instance.root_password_hash != original_hash
    assert not verify_password(original, instance.root_password_hash)


# --- state machine ----------------------------------------------------------


def test_cannot_start_a_running_instance(db, catalog, account):
    instance, job, _ = order(db, account)
    provisioning.run_job(db, job, MockDriver())

    with pytest.raises(InvalidStateError, match="Cannot start"):
        provisioning.request_action(db, instance=instance, job_type=JobType.START)


def test_cannot_reboot_a_stopped_instance(db, catalog, account):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)
    stop = provisioning.request_action(db, instance=instance, job_type=JobType.STOP)
    provisioning.run_job(db, stop, driver)

    with pytest.raises(InvalidStateError):
        provisioning.request_action(db, instance=instance, job_type=JobType.REBOOT)


def test_concurrent_actions_on_one_instance_are_rejected(db, catalog, account):
    """A second power action must not start while one is already in flight."""
    instance, job, _ = order(db, account)
    provisioning.run_job(db, job, MockDriver())

    # SUSPEND has no in-flight status of its own, so the instance stays RUNNING
    # and the next request is blocked by the concurrency guard rather than by the
    # state-transition rules.
    provisioning.request_action(db, instance=instance, job_type=JobType.SUSPEND)
    assert instance.status is InstanceStatus.RUNNING

    with pytest.raises(InvalidStateError, match="already running"):
        provisioning.request_action(db, instance=instance, job_type=JobType.STOP)


# --- failure handling -------------------------------------------------------


def test_permanent_provisioning_failure_releases_the_ip(db, catalog, account):
    """A VM that never existed must not hold an address hostage."""
    instance, job, _ = order(db, account)
    before = ipam.region_available_count(db, catalog["region"].id)
    assert before == 4  # one taken by the order

    driver = MockDriver(fail_create=True, fail_permanently=True)
    with pytest.raises(DriverError):
        provisioning.run_job(db, job, driver)

    provisioning.fail_job(db, job, "Simulated provisioning failure", final=True)

    assert job.status is JobStatus.FAILED
    assert instance.status is InstanceStatus.ERROR
    assert instance.error_message
    assert ipam.region_available_count(db, catalog["region"].id) == 5


def test_non_final_failure_keeps_the_ip_for_the_retry(db, catalog, account):
    instance, job, _ = order(db, account)

    provisioning.fail_job(db, job, "transient vCenter error", final=False)

    assert job.status is not JobStatus.FAILED
    assert job.error == "transient vCenter error"
    assert instance.status is InstanceStatus.PENDING
    # The address stays reserved so the retry keeps the same IP.
    assert ipam.region_available_count(db, catalog["region"].id) == 4


def test_provision_is_idempotent_on_retry(db, catalog, account):
    """A retried provision adopts the existing VM instead of cloning a second one."""
    instance, job, _ = order(db, account)
    driver = MockDriver()

    provisioning.run_job(db, job, driver)
    first_ref = instance.hypervisor_ref

    job.status = JobStatus.QUEUED
    provisioning.run_job(db, job, driver)

    assert instance.hypervisor_ref == first_ref
    assert len(MockDriver.all_vms()) == 1


# --- reconciliation ---------------------------------------------------------


def test_reconcile_corrects_out_of_band_power_off(db, catalog, account):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    # Somebody powers the VM off directly in vCenter.
    MockDriver.force_power_state(instance.hypervisor_ref, PowerState.OFF)

    result = provisioning.reconcile_instance(db, instance, driver)

    assert result["changed"] is True
    assert instance.status is InstanceStatus.STOPPED
    assert instance.drift_detected_at is not None


def test_reconcile_flags_a_vm_that_vanished(db, catalog, account):
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    MockDriver.force_delete(instance.hypervisor_ref)

    result = provisioning.reconcile_instance(db, instance, driver)

    assert result["reason"] == "vm_missing"
    assert instance.status is InstanceStatus.ERROR


def test_reconcile_adopts_an_orphaned_vm(db, catalog, account):
    """Recovers a VM created just before the worker died, before the ref was saved."""
    instance, _job, _ = order(db, account)
    driver = MockDriver()
    spec = provisioning.build_spec(db, instance, root_password="x")
    created = driver.create_instance(spec)

    assert instance.hypervisor_ref is None

    result = provisioning.reconcile_instance(db, instance, driver)

    assert result["adopted_ref"] == created.ref
    assert instance.hypervisor_ref == created.ref


def test_reconcile_leaves_transitional_states_alone(db, catalog, account):
    """Correcting a mid-flight instance would race the worker driving it."""
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    instance.status = InstanceStatus.STOPPING
    db.flush()
    MockDriver.force_power_state(instance.hypervisor_ref, PowerState.ON)

    result = provisioning.reconcile_instance(db, instance, driver)

    assert result["changed"] is False
    assert instance.status is InstanceStatus.STOPPING


def test_reconcile_does_not_unsuspend_a_suspended_instance(db, catalog, account):
    """A suspended instance is intentionally powered off; that is not drift."""
    instance, job, _ = order(db, account)
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    suspend = provisioning.request_action(db, instance=instance, job_type=JobType.SUSPEND)
    provisioning.run_job(db, suspend, driver)
    assert instance.status is InstanceStatus.SUSPENDED

    result = provisioning.reconcile_instance(db, instance, driver)

    assert result["changed"] is False
    assert instance.status is InstanceStatus.SUSPENDED


def test_find_stale_instances_skips_deleted(db, catalog, account):
    instance, job, _ = order(db, account)
    provisioning.run_job(db, job, MockDriver())
    instance.last_synced_at = utcnow() - timedelta(hours=2)
    db.flush()

    stale = provisioning.find_stale_instances(db, utcnow() - timedelta(minutes=15))
    assert instance in stale

    instance.status = InstanceStatus.DELETED
    db.flush()

    assert instance not in provisioning.find_stale_instances(db, utcnow() - timedelta(minutes=15))
