"""IP address management.

Replaces the prototype's `ip_scanner.get_free_ip()`, which pinged a /24 one host
at a time (up to ~254s inside a web request) and treated "did not answer ICMP" as
"unallocated" — so two simultaneous orders would happily receive the same address,
and any host with a firewall was silently double-assigned.

Allocation here is a single indexed query under `FOR UPDATE SKIP LOCKED`: rows a
concurrent transaction already holds are skipped rather than waited on, so N
parallel provisioners get N distinct addresses with no retry loop and no lock
convoy.
"""

from __future__ import annotations

import ipaddress
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import IPStatus, IPVersion
from app.models.network import IpAddress, IpPool
from app.security import utcnow
from app.services.errors import ConflictError, NoCapacityError, ValidationError

# Guards against someone seeding a /8 and materialising 16M rows by accident.
MAX_POOL_HOSTS = 65536


def expand_pool(
    session: Session,
    pool: IpPool,
    *,
    skip_network_and_broadcast: bool = True,
    reserve: list[str] | None = None,
) -> int:
    """Materialise every usable host address in `pool.cidr` as a row.

    Idempotent: addresses that already exist are left untouched, so re-running
    after widening a pool only adds what is missing. Returns the number created.
    """
    network = ipaddress.ip_network(pool.cidr, strict=False)
    if network.num_addresses > MAX_POOL_HOSTS:
        raise ValidationError(
            f"Pool {pool.cidr} has {network.num_addresses} addresses, "
            f"above the {MAX_POOL_HOSTS} limit for materialisation"
        )

    hosts = list(network.hosts()) if skip_network_and_broadcast else list(network)

    reserved = {str(a) for a in (reserve or [])}
    if pool.gateway:
        reserved.add(pool.gateway)

    existing = {
        row[0]
        for row in session.execute(
            select(IpAddress.address).where(IpAddress.pool_id == pool.id)
        ).all()
    }

    created = 0
    for host in hosts:
        addr = str(host)
        if addr in existing:
            continue
        session.add(
            IpAddress(
                pool_id=pool.id,
                address=addr,
                # Gateway and explicitly reserved addresses are materialised but
                # never allocatable, so they stay visible in the admin UI instead
                # of being invisible gaps.
                status=IPStatus.RESERVED if addr in reserved else IPStatus.AVAILABLE,
                is_primary=True,
                notes="reserved at pool creation" if addr in reserved else None,
            )
        )
        created += 1

    session.flush()
    return created


def allocate_ip(
    session: Session,
    *,
    region_id: str,
    instance_id: str,
    version: IPVersion = IPVersion.V4,
) -> IpAddress:
    """Claim one free address in `region_id` and bind it to `instance_id`.

    Must run inside the caller's transaction: the row lock taken here is what
    guarantees exclusivity, and it is only held until that transaction commits.
    """
    now = utcnow()

    stmt = (
        select(IpAddress)
        .join(IpPool, IpAddress.pool_id == IpPool.id)
        .where(
            IpPool.region_id == region_id,
            IpPool.is_active.is_(True),
            IpPool.version == version,
            IpAddress.status == IPStatus.AVAILABLE,
            IpAddress.instance_id.is_(None),
            # Quarantined addresses become available again only once their
            # cool-down has elapsed.
            (IpAddress.reusable_at.is_(None)) | (IpAddress.reusable_at <= now),
        )
        .order_by(IpAddress.address)
        .limit(1)
        .with_for_update(skip_locked=True, of=IpAddress)
    )

    address = session.execute(stmt).scalars().first()
    if address is None:
        raise NoCapacityError(
            "No free IP addresses are available in this region",
            details={"region_id": region_id, "version": str(version)},
        )

    address.status = IPStatus.ASSIGNED
    address.instance_id = instance_id
    address.allocated_at = now
    address.released_at = None
    address.reusable_at = None
    session.flush()
    return address


def release_ip(session: Session, address: IpAddress, *, quarantine: bool = True) -> None:
    """Return an address to its pool, optionally after a quarantine period."""
    settings = get_settings()
    now = utcnow()

    address.instance_id = None
    address.released_at = now

    if quarantine and settings.ip_quarantine_hours > 0:
        address.status = IPStatus.QUARANTINED
        address.reusable_at = now + timedelta(hours=settings.ip_quarantine_hours)
    else:
        address.status = IPStatus.AVAILABLE
        address.reusable_at = None

    session.flush()


def release_instance_ips(session: Session, instance_id: str, *, quarantine: bool = True) -> int:
    """Release every address bound to an instance. Returns how many were freed."""
    addresses = (
        session.execute(select(IpAddress).where(IpAddress.instance_id == instance_id))
        .scalars()
        .all()
    )
    for address in addresses:
        release_ip(session, address, quarantine=quarantine)
    return len(addresses)


def sweep_quarantine(session: Session) -> int:
    """Return quarantined addresses whose cool-down has elapsed to the free pool.

    Allocation already tolerates quarantined-but-elapsed rows, so this is
    housekeeping for accurate capacity reporting rather than a correctness
    requirement.
    """
    now = utcnow()
    rows = (
        session.execute(
            select(IpAddress)
            .where(
                IpAddress.status == IPStatus.QUARANTINED,
                IpAddress.reusable_at.is_not(None),
                IpAddress.reusable_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = IPStatus.AVAILABLE
        row.reusable_at = None
    session.flush()
    return len(rows)


def reserve_ip(session: Session, address: IpAddress, note: str | None = None) -> None:
    if address.status == IPStatus.ASSIGNED:
        raise ConflictError("Cannot reserve an address that is assigned to an instance")
    address.status = IPStatus.RESERVED
    address.notes = note
    session.flush()


def unreserve_ip(session: Session, address: IpAddress) -> None:
    if address.status != IPStatus.RESERVED:
        raise ConflictError("Address is not reserved")
    address.status = IPStatus.AVAILABLE
    address.notes = None
    session.flush()


def pool_capacity(session: Session, pool_id: str) -> dict[str, int]:
    """Per-status counts for one pool."""
    rows = session.execute(
        select(IpAddress.status, func.count())
        .where(IpAddress.pool_id == pool_id)
        .group_by(IpAddress.status)
    ).all()
    counts = {str(status): count for status, count in rows}
    return {
        "total": sum(counts.values()),
        "available": counts.get(IPStatus.AVAILABLE, 0),
        "assigned": counts.get(IPStatus.ASSIGNED, 0),
        "reserved": counts.get(IPStatus.RESERVED, 0),
        "quarantined": counts.get(IPStatus.QUARANTINED, 0),
    }


def region_available_count(
    session: Session, region_id: str, version: IPVersion = IPVersion.V4
) -> int:
    """How many addresses could be allocated in a region right now."""
    now = utcnow()
    return int(
        session.execute(
            select(func.count())
            .select_from(IpAddress)
            .join(IpPool, IpAddress.pool_id == IpPool.id)
            .where(
                IpPool.region_id == region_id,
                IpPool.is_active.is_(True),
                IpPool.version == version,
                IpAddress.status == IPStatus.AVAILABLE,
                IpAddress.instance_id.is_(None),
                (IpAddress.reusable_at.is_(None)) | (IpAddress.reusable_at <= now),
            )
        ).scalar_one()
    )


def netmask_for(pool: IpPool) -> str:
    """Dotted-quad netmask for an IPv4 pool, for guests that want one."""
    if pool.version != IPVersion.V4:
        raise ValidationError("Netmask is only meaningful for IPv4 pools")
    return str(ipaddress.IPv4Network(f"0.0.0.0/{pool.prefix_length}").netmask)
