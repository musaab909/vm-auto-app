"""IPAM tests.

The headline test is `test_concurrent_allocation_never_double_issues`, which is
the property the prototype's ping sweep got wrong: it used "does not answer ICMP"
as a proxy for "unallocated", so two simultaneous orders were handed the same
address. This exercises genuinely concurrent transactions against real Postgres.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.enums import IPStatus
from app.models.network import IpAddress, IpPool
from app.services import ipam
from app.services.errors import NoCapacityError, ValidationError


def test_expand_pool_materialises_hosts_and_reserves_gateway(db, pool):
    addresses = db.execute(select(IpAddress).where(IpAddress.pool_id == pool.id)).scalars().all()

    # A /29 has 6 usable hosts.
    assert len(addresses) == 6

    gateway = next(a for a in addresses if a.address == "198.51.100.1")
    assert gateway.status is IPStatus.RESERVED, "the gateway must never be allocatable"

    assert sum(1 for a in addresses if a.status is IPStatus.AVAILABLE) == 5


def test_expand_pool_is_idempotent(db, pool):
    before = db.execute(
        select(func.count()).select_from(IpAddress).where(IpAddress.pool_id == pool.id)
    ).scalar_one()

    created = ipam.expand_pool(db, pool)

    after = db.execute(
        select(func.count()).select_from(IpAddress).where(IpAddress.pool_id == pool.id)
    ).scalar_one()

    assert created == 0
    assert after == before


def test_expand_pool_rejects_oversized_networks(db, region):
    pool = IpPool(
        region_id=region.id, name="huge", cidr="10.0.0.0/8", prefix_length=8, gateway=None
    )
    db.add(pool)
    db.flush()

    with pytest.raises(ValidationError, match="above the"):
        ipam.expand_pool(db, pool)


def test_allocate_marks_assigned_and_binds_instance(db, pool, make_instance):
    instance = make_instance()

    address = ipam.allocate_ip(db, region_id=pool.region_id, instance_id=instance.id)

    assert address.status is IPStatus.ASSIGNED
    assert address.instance_id == instance.id
    assert address.allocated_at is not None
    assert address.address != "198.51.100.1", "must not hand out the gateway"


def test_allocation_exhausts_pool_then_raises(db, pool, make_instance):
    for i in range(5):
        ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance(f"vm-{i}").id)

    with pytest.raises(NoCapacityError):
        ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance("extra").id)


def test_release_returns_address_to_pool(db, pool, make_instance):
    address = ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance().id)

    ipam.release_ip(db, address, quarantine=False)

    assert address.status is IPStatus.AVAILABLE
    assert address.instance_id is None
    assert address.released_at is not None

    # Immediately reusable when quarantine is off.
    reallocated = ipam.allocate_ip(
        db, region_id=pool.region_id, instance_id=make_instance("vm-2").id
    )
    assert reallocated.id == address.id


def test_quarantined_address_is_not_immediately_reissued(db, pool, make_instance, monkeypatch):
    """A released address must cool down before another customer inherits it."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ip_quarantine_hours", 24)

    address = ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance().id)
    freed = address.address
    ipam.release_ip(db, address, quarantine=True)

    assert address.status is IPStatus.QUARANTINED
    assert address.reusable_at is not None

    # The remaining four free addresses are handed out; the quarantined one is not.
    handed_out = {
        ipam.allocate_ip(
            db, region_id=pool.region_id, instance_id=make_instance(f"vm-{i}").id
        ).address
        for i in range(4)
    }
    assert freed not in handed_out

    with pytest.raises(NoCapacityError):
        ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance("blocked").id)


def test_sweep_quarantine_releases_elapsed_addresses(db, pool, make_instance):
    from datetime import timedelta

    from app.security import utcnow

    address = ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance().id)
    ipam.release_ip(db, address, quarantine=False)
    address.status = IPStatus.QUARANTINED
    address.reusable_at = utcnow() - timedelta(hours=1)
    db.flush()

    assert ipam.sweep_quarantine(db) == 1
    assert address.status is IPStatus.AVAILABLE


def test_release_instance_ips_frees_all_bound_addresses(db, pool, make_instance):
    instance = make_instance()
    ipam.allocate_ip(db, region_id=pool.region_id, instance_id=instance.id)
    db.commit()

    assert ipam.release_instance_ips(db, instance.id, quarantine=False) == 1
    assert ipam.region_available_count(db, pool.region_id) == 5


def test_region_available_count_tracks_allocation(db, pool, make_instance):
    assert ipam.region_available_count(db, pool.region_id) == 5

    ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance().id)

    assert ipam.region_available_count(db, pool.region_id) == 4


def test_pool_capacity_breaks_down_by_status(db, pool, make_instance):
    ipam.allocate_ip(db, region_id=pool.region_id, instance_id=make_instance().id)
    db.commit()

    stats = ipam.pool_capacity(db, pool.id)

    assert stats == {
        "total": 6,
        "available": 4,
        "assigned": 1,
        "reserved": 1,  # the gateway
        "quarantined": 0,
    }


def test_netmask_for_ipv4_pool(db, pool):
    assert ipam.netmask_for(pool) == "255.255.255.248"


def test_concurrent_allocation_never_double_issues(db, pool, make_instance):
    """Five threads allocate at once; each must get a distinct address.

    This is the exact failure the ping-sweep scanner had. Each thread runs its own
    transaction, so the only thing preventing a collision is `FOR UPDATE SKIP
    LOCKED` in the allocator.
    """
    instance_ids = [make_instance(f"vm-{i}").id for i in range(5)]
    db.commit()  # make the pool and instances visible to other connections

    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    start = threading.Barrier(5)

    def allocate(index: int) -> None:
        session = SessionLocal()
        try:
            start.wait(timeout=10)  # maximise real contention
            address = ipam.allocate_ip(
                session, region_id=pool.region_id, instance_id=instance_ids[index]
            )
            session.commit()
            with lock:
                results.append(address.address)
        except Exception as exc:
            session.rollback()
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=allocate, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"allocation raised under contention: {errors}"
    assert len(results) == 5
    assert len(set(results)) == 5, f"the same address was issued twice: {results}"


def test_concurrent_allocation_beyond_capacity_fails_cleanly(db, pool, make_instance):
    """With 5 free addresses and 8 racers, exactly 5 win and 3 get NoCapacity."""
    instance_ids = [make_instance(f"vm-{i}").id for i in range(8)]
    db.commit()

    allocated: list[str] = []
    capacity_errors: list[Exception] = []
    other_errors: list[Exception] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def allocate(index: int) -> None:
        session = SessionLocal()
        try:
            start.wait(timeout=10)
            address = ipam.allocate_ip(
                session, region_id=pool.region_id, instance_id=instance_ids[index]
            )
            session.commit()
            with lock:
                allocated.append(address.address)
        except NoCapacityError as exc:
            session.rollback()
            with lock:
                capacity_errors.append(exc)
        except Exception as exc:
            session.rollback()
            with lock:
                other_errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=allocate, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not other_errors, f"unexpected failures: {other_errors}"
    assert len(set(allocated)) == len(allocated) == 5
    assert len(capacity_errors) == 3
