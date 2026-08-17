"""Operational CLI: seeding, admin bootstrap and manual billing runs.

Run as ``python -m app.cli <command>`` or via the ``hostplane`` entry point.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import session_scope
from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.enums import IPVersion, UserRole
from app.models.identity import Account, AccountMember, User
from app.models.network import IpPool
from app.security import hash_password, utcnow
from app.services import ipam, quota

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
