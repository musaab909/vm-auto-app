"""Operational CLI: seeding, admin bootstrap and manual billing runs.

Run as ``python -m app.cli <command>`` or via the ``hostplane`` entry point.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db import session_scope
from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.enums import IPVersion, UserRole
from app.models.identity import Account, AccountMember, User
from app.models.network import IpPool
from app.security import hash_password, utcnow
from app.services import ipam, quota
from app.services.errors import DriverError

# A starter catalog modelled on typical VPS tiers. Prices are in cents.
DEFAULT_PLANS: list[dict] = [
    {
        "slug": "vps-s",
        "name": "VPS S",
        "vcpu": 2,
        "ram_mb": 4096,
        "disk_gb": 50,
        "bandwidth_tb": 32,
        "price_cents": 599,
        "sort_order": 1,
        "description": "Small workloads, dev boxes and low-traffic sites.",
    },
    {
        "slug": "vps-m",
        "name": "VPS M",
        "vcpu": 4,
        "ram_mb": 8192,
        "disk_gb": 100,
        "bandwidth_tb": 32,
        "price_cents": 1099,
        "sort_order": 2,
        "description": "Application servers and small databases.",
    },
    {
        "slug": "vps-l",
        "name": "VPS L",
        "vcpu": 6,
        "ram_mb": 16384,
        "disk_gb": 200,
        "bandwidth_tb": 32,
        "price_cents": 1999,
        "sort_order": 3,
        "description": "Busy applications and CI runners.",
    },
    {
        "slug": "vps-xl",
        "name": "VPS XL",
        "vcpu": 8,
        "ram_mb": 32768,
        "disk_gb": 400,
        "bandwidth_tb": 32,
        "price_cents": 3499,
        "sort_order": 4,
        "description": "Heavy workloads and multi-service hosts.",
    },
]

DEFAULT_IMAGES: list[dict] = [
    {
        "slug": "ubuntu-24-04",
        "name": "Ubuntu 24.04 LTS",
        "family": "ubuntu",
        "version": "24.04",
        "vsphere_template": "tmpl-ubuntu-24.04",
        "min_disk_gb": 10,
        "sort_order": 1,
    },
    {
        "slug": "debian-12",
        "name": "Debian 12",
        "family": "debian",
        "version": "12",
        "vsphere_template": "tmpl-debian-12",
        "min_disk_gb": 10,
        "sort_order": 2,
    },
    {
        "slug": "rocky-9",
        "name": "Rocky Linux 9",
        "family": "rocky",
        "version": "9",
        "vsphere_template": "tmpl-rocky-9",
        "min_disk_gb": 20,
        "sort_order": 3,
    },
    {
        "slug": "almalinux-9",
        "name": "AlmaLinux 9",
        "family": "alma",
        "version": "9",
        "vsphere_template": "tmpl-alma-9",
        "min_disk_gb": 20,
        "sort_order": 4,
    },
]

DEFAULT_REGIONS: list[dict] = [
    {
        "slug": "eu-central",
        "name": "Frankfurt, Germany",
        "country": "DE",
        "vsphere_datacenter": "DC-Frankfurt",
        "vsphere_cluster": "Cluster-01",
        "vsphere_datastore": "vsanDatastore",
        "vsphere_network": "VM Network",
        "vsphere_folder": None,
    },
]

DEFAULT_POOLS: list[dict] = [
    {
        "region": "eu-central",
        "name": "Frankfurt public /24",
        # Documentation range (RFC 5737) so a fresh install cannot hand out
        # somebody else's addresses before an operator configures a real pool.
        "cidr": "203.0.113.0/24",
        "prefix_length": 24,
        "gateway": "203.0.113.1",
        "vsphere_network": "VM Network",
        "vlan_id": 100,
    },
]


def cmd_seed(args: argparse.Namespace) -> int:
    """Idempotently create the starter catalog, region and IP pool."""
    with session_scope() as session:
        created = {"regions": 0, "plans": 0, "images": 0, "pools": 0, "addresses": 0}

        regions: dict[str, Region] = {}
        for spec in DEFAULT_REGIONS:
            region = session.execute(
                select(Region).where(Region.slug == spec["slug"])
            ).scalar_one_or_none()
            if region is None:
                region = Region(**spec)
                session.add(region)
                session.flush()
                created["regions"] += 1
            regions[region.slug] = region

        for spec in DEFAULT_PLANS:
            plan = session.execute(
                select(Plan).where(Plan.slug == spec["slug"])
            ).scalar_one_or_none()
            if plan is None:
                plan = Plan(**spec)
                session.add(plan)
                session.flush()
                created["plans"] += 1

            # Offer every plan in every region unless already linked.
            for region in regions.values():
                link = session.execute(
                    select(PlanRegion).where(
                        PlanRegion.plan_id == plan.id, PlanRegion.region_id == region.id
                    )
                ).scalar_one_or_none()
                if link is None:
                    session.add(PlanRegion(plan_id=plan.id, region_id=region.id))

        for spec in DEFAULT_IMAGES:
            image = session.execute(
                select(OsImage).where(OsImage.slug == spec["slug"])
            ).scalar_one_or_none()
            if image is None:
                session.add(OsImage(**spec))
                created["images"] += 1

        for spec in DEFAULT_POOLS:
            region = regions.get(spec["region"])
            if region is None:
                continue
            pool = session.execute(
                select(IpPool).where(IpPool.region_id == region.id, IpPool.cidr == spec["cidr"])
            ).scalar_one_or_none()
            if pool is None:
                pool = IpPool(
                    region_id=region.id,
                    name=spec["name"],
                    cidr=spec["cidr"],
                    version=IPVersion.V4,
                    gateway=spec["gateway"],
                    prefix_length=spec["prefix_length"],
                    vsphere_network=spec["vsphere_network"],
                    vlan_id=spec["vlan_id"],
                )
                session.add(pool)
                session.flush()
                created["pools"] += 1

            # Materialise the addresses; already-present rows are left alone.
            created["addresses"] += ipam.expand_pool(session, pool)

        print("Seeded:")
        for key, value in created.items():
            print(f"  {key}: {value}")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    with session_scope() as session:
        email = args.email.strip().lower()
        existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing is not None:
            existing.role = UserRole.ADMIN
            existing.email_verified_at = existing.email_verified_at or utcnow()
            print(f"Promoted existing user {email} to administrator.")
            return 0

        user = User(
            email=email,
            password_hash=hash_password(args.password),
            full_name=args.name,
            role=UserRole.ADMIN,
            email_verified_at=utcnow(),
        )
        session.add(user)
        session.flush()

        account = Account(name=args.name or email, owner_user_id=user.id, billing_email=email)
        session.add(account)
        session.flush()
        session.add(AccountMember(account_id=account.id, user_id=user.id, role="owner"))
        quota.get_or_create_quota(session, account)

        print(f"Created administrator {email}.")
    return 0


def cmd_run_billing(args: argparse.Namespace) -> int:
    """Run renewals and dunning immediately instead of waiting for beat."""
    from app.workers.tasks import run_dunning, run_renewals

    print("Renewals:", run_renewals())
    print("Dunning:", run_dunning())
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from app.workers.tasks import reconcile

    print("Reconcile:", reconcile())
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    with session_scope() as session:
        for region in session.execute(select(Region).order_by(Region.name)).scalars():
            available = ipam.region_available_count(session, region.id)
            print(f"{region.slug:<14} {region.name:<26} {available} IPs available")
            for pool in session.execute(
                select(IpPool).where(IpPool.region_id == region.id)
            ).scalars():
                stats = ipam.pool_capacity(session, pool.id)
                print(
                    f"    {pool.cidr:<20} total={stats['total']:<5} "
                    f"available={stats['available']:<5} assigned={stats['assigned']:<5} "
                    f"reserved={stats['reserved']:<4} quarantined={stats['quarantined']}"
                )
    return 0


_STATUS_GLYPH = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that this deployment could actually provision a server.

    Exits non-zero when any check fails, so it can gate a deploy.
    """
    from app.config import get_settings
    from app.drivers.preflight import run_preflight

    settings = get_settings()

    if args.dry_run:
        return _dry_run_cloud_init()

    driver = None
    if settings.driver == "vsphere":
        from app.drivers.vsphere import VSphereDriver

        try:
            driver = VSphereDriver()
        except DriverError as exc:
            # A misconfiguration is a finding, not a crash.
            print(f"{_STATUS_GLYPH['fail']}  vCenter configuration: {exc.message}")
            print("      fix: set HOSTPLANE_VSPHERE_HOST, _USER and _PASSWORD.")
            return 1

    try:
        with session_scope() as session:
            report = run_preflight(session, driver)
    except OperationalError as exc:
        # The database is the one dependency doctor cannot check *through*, so
        # report it as a finding rather than letting a driver traceback escape.
        print(f"{_STATUS_GLYPH['fail']}  Database connection: {exc.orig or exc}")
        print("      fix: check HOSTPLANE_DATABASE_URL and that PostgreSQL is running.")
        return 1

    width = max((len(c.name) for c in report.checks), default=20)
    for check in report.checks:
        print(f"{_STATUS_GLYPH[check.status.value]}  {check.name:<{width}}  {check.detail}")
        if check.remedy:
            print(f"      fix: {check.remedy}")

    counts = report.counts()
    print(
        f"\n{counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['warn']} warnings, {counts['skip']} skipped"
    )

    if report.passed:
        print(
            "\nNo blocking problems found. This does not prove a guest boots or "
            "gets network — order one server and check it before opening for business."
        )
        return 0

    print(f"\n{len(report.failures)} check(s) must be fixed before provisioning will work.")
    return 1


def _dry_run_cloud_init() -> int:
    """Print the cloud-init documents a real order would inject.

    Builds them from the seeded catalog without contacting vCenter, so the exact
    payload can be reviewed before any VM exists.
    """
    import base64

    from app.drivers.base import InstanceSpec, NetworkSpec
    from app.drivers.vsphere import build_metadata, build_userdata

    with session_scope() as session:
        plan = (
            session.execute(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order))
            .scalars()
            .first()
        )
        image = (
            session.execute(
                select(OsImage).where(OsImage.is_active.is_(True)).order_by(OsImage.sort_order)
            )
            .scalars()
            .first()
        )
        region = session.execute(select(Region).where(Region.is_active.is_(True))).scalars().first()

        if not (plan and image and region):
            print("Nothing to render: seed the catalog first (`hostplane seed`).")
            return 1

        pool = (
            session.execute(select(IpPool).where(IpPool.region_id == region.id)).scalars().first()
        )

        spec = InstanceSpec(
            name="dry-run-example",
            hostname="web-01",
            vcpu=plan.vcpu,
            ram_mb=plan.ram_mb,
            disk_gb=plan.disk_gb,
            template=image.vsphere_template or image.slug,
            network=NetworkSpec(
                ip_address="203.0.113.10",
                prefix_length=pool.prefix_length if pool else 24,
                gateway=pool.gateway if pool else "203.0.113.1",
                dns_servers=pool.dns_list if pool else ["1.1.1.1"],
                network_name=(pool.vsphere_network if pool else None) or region.vsphere_network,
            ),
            datacenter=region.vsphere_datacenter,
            cluster=region.vsphere_cluster,
            resource_pool=region.vsphere_resource_pool,
            datastore=region.vsphere_datastore,
            folder=region.vsphere_folder,
            root_password="<generated-at-order-time>",
            ssh_keys=[],
            default_username=image.default_username,
            instance_id="00000000-0000-0000-0000-000000000000",
            account_id="00000000-0000-0000-0000-000000000000",
        )

        print(f"Plan {plan.slug} · image {image.slug} · region {region.slug}\n")
        print("=== placement ===")
        for label, value in (
            ("datacenter", spec.datacenter),
            ("cluster", spec.cluster),
            ("resource pool", spec.resource_pool),
            ("datastore", spec.datastore),
            ("folder", spec.folder),
            ("template", spec.template),
            ("port group", spec.network.network_name),
        ):
            print(f"  {label:<14} {value or '(default)'}")

        metadata = build_metadata(spec)
        print("\n=== guestinfo.metadata (decoded) ===")
        print(metadata)

        # The network document is nested and base64-encoded inside the metadata,
        # so decode it too — that nesting is exactly where it used to go wrong.
        for line in metadata.splitlines():
            if line.startswith("network: "):
                print("=== metadata.network (decoded) ===")
                print(base64.b64decode(line.split(" ", 1)[1]).decode())
                break

        print("=== guestinfo.userdata (decoded) ===")
        print(build_userdata(spec))

        print("=== guestinfo keys that will be written ===")
        for key in (
            "guestinfo.metadata",
            "guestinfo.metadata.encoding",
            "guestinfo.userdata",
            "guestinfo.userdata.encoding",
            "guestinfo.hostplane.instance_id",
            "guestinfo.hostplane.account_id",
        ):
            print(f"  {key}")
        print(
            "\nNo guestinfo.vendordata is written: cloud-init reads it as "
            "cloud-config, so network config lives inside metadata instead."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hostplane", description="HostPlane operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Create the starter catalog, region and IP pool").set_defaults(
        func=cmd_seed
    )

    admin = sub.add_parser("create-admin", help="Create or promote an administrator")
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", required=True)
    admin.add_argument("--name", default=None)
    admin.set_defaults(func=cmd_create_admin)

    sub.add_parser("run-billing", help="Run renewals and dunning now").set_defaults(
        func=cmd_run_billing
    )
    sub.add_parser("reconcile", help="Reconcile instances against the hypervisor").set_defaults(
        func=cmd_reconcile
    )
    sub.add_parser("capacity", help="Show IP capacity per region").set_defaults(func=cmd_capacity)

    doctor = sub.add_parser(
        "doctor",
        help="Check this deployment can actually provision (run before going live)",
    )
    doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cloud-init documents a real order would inject, without contacting vCenter",
    )
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
