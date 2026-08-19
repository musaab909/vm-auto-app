"""Clone concurrency limiting.

vCenter throttles concurrent clones per datastore and host; past the limit it
answers `Resource in use`. Without a cap in the control plane a burst of orders
becomes a pile of failed jobs instead of a queue, so the limit is enforced before
any hypervisor work is attempted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.drivers.mock import MockDriver
from app.models.catalog import Region
from app.models.enums import InstanceStatus, JobStatus
from app.services import provisioning
from app.services.errors import DriverError


def order(db, account, label):
    return provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label=label,
    )


def set_limit(db, value):
    region = db.execute(select(Region).where(Region.slug == "eu-central")).scalar_one()
    region.max_concurrent_clones = value
    db.flush()
    return region


def test_regions_default_to_a_finite_clone_limit(db, catalog):
    region = db.execute(select(Region).where(Region.slug == "eu-central")).scalar_one()
    assert region.max_concurrent_clones > 0


def test_slot_count_tracks_running_provisions(db, catalog, account):
    region = set_limit(db, 4)
    assert provisioning.clone_slots_in_use(db, region.id) == 0

    _instance, job, _ = order(db, account, "web-01")
    job.status = JobStatus.RUNNING
    db.flush()

    assert provisioning.clone_slots_in_use(db, region.id) == 1

    # A job excludes itself, so it never counts against its own limit.
    assert provisioning.clone_slots_in_use(db, region.id, exclude_job_id=job.id) == 0


def test_a_second_clone_is_deferred_at_the_limit(db, catalog, account):
    """The deferral must happen before the driver is touched."""
    set_limit(db, 1)
    driver = MockDriver()

    _first, first_job, _ = order(db, account, "web-01")
    first_job.status = JobStatus.RUNNING  # in flight
    db.flush()

    _second, second_job, _ = order(db, account, "web-02")

    with pytest.raises(DriverError) as exc:
        provisioning.run_job(db, second_job, driver)

    assert "clone limit" in exc.value.message
    # Retryable, so the worker's existing backoff re-queues it.
    assert exc.value.permanent is False
    # And crucially: no VM was created for the deferred order.
    assert len(MockDriver.all_vms()) == 0


def test_a_deferred_clone_succeeds_once_a_slot_frees(db, catalog, account):
    set_limit(db, 1)
    driver = MockDriver()

    _first, first_job, _ = order(db, account, "web-01")
    first_job.status = JobStatus.RUNNING
    db.flush()

    second, second_job, _ = order(db, account, "web-02")
    with pytest.raises(DriverError):
        provisioning.run_job(db, second_job, driver)

    # The first clone finishes and releases its slot.
    first_job.status = JobStatus.SUCCEEDED
    second_job.status = JobStatus.QUEUED
    db.flush()

    provisioning.run_job(db, second_job, driver)

    assert second.status is InstanceStatus.RUNNING
    assert second_job.status is JobStatus.SUCCEEDED


def test_under_the_limit_a_clone_proceeds(db, catalog, account):
    set_limit(db, 2)
    driver = MockDriver()

    _first, first_job, _ = order(db, account, "web-01")
    first_job.status = JobStatus.RUNNING
    db.flush()

    second, second_job, _ = order(db, account, "web-02")
    provisioning.run_job(db, second_job, driver)

    assert second.status is InstanceStatus.RUNNING


def test_zero_means_unlimited(db, catalog, account):
    """Operators who throttle at the hypervisor should be able to opt out."""
    set_limit(db, 0)
    driver = MockDriver()

    _first, first_job, _ = order(db, account, "web-01")
    first_job.status = JobStatus.RUNNING
    db.flush()

    second, second_job, _ = order(db, account, "web-02")
    provisioning.run_job(db, second_job, driver)

    assert second.status is InstanceStatus.RUNNING


def test_the_limit_is_per_region(db, catalog, account, region):
    """A busy region must not stall a quiet one."""
    from app.models.catalog import PlanRegion
    from app.models.enums import IPVersion
    from app.models.network import IpPool
    from app.services import ipam

    set_limit(db, 1)

    other = Region(slug="eu-west", name="Amsterdam", vsphere_cluster="C2")
    db.add(other)
    db.flush()
    other.max_concurrent_clones = 1

    plan = catalog["plan"]
    db.add(PlanRegion(plan_id=plan.id, region_id=other.id))
    pool = IpPool(
        region_id=other.id,
        name="second",
        cidr="192.0.2.0/29",
        version=IPVersion.V4,
        gateway="192.0.2.1",
        prefix_length=29,
    )
    db.add(pool)
    db.flush()
    ipam.expand_pool(db, pool)

    _busy, busy_job, _ = order(db, account, "web-01")
    busy_job.status = JobStatus.RUNNING
    db.flush()

    # eu-central is saturated; eu-west has a free slot.
    assert provisioning.clone_slots_in_use(db, region.id) == 1
    assert provisioning.clone_slots_in_use(db, other.id) == 0

    quiet, quiet_job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-west",
        label="web-02",
    )
    provisioning.run_job(db, quiet_job, MockDriver())

    assert quiet.status is InstanceStatus.RUNNING
