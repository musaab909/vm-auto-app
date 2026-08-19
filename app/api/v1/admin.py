"""Staff and administrator routes.

Everything here is gated on `StaffUser`/`AdminUser` and is deliberately outside
the account-scoped ownership rules that apply to customer routes.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession, StaffUser
from app.models.billing import Invoice, Subscription
from app.models.catalog import Plan, Region
from app.models.compute import Instance, Job
from app.models.enums import (
    InstanceStatus,
    InvoiceStatus,
    IPStatus,
    JobStatus,
    SubscriptionStatus,
)
from app.models.identity import Account, User
from app.models.network import IpAddress, IpPool
from app.services import ipam

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
def platform_stats(session: DbSession, user: StaffUser):
    """Operational overview for the staff dashboard."""

    def count(model, *where):
        return int(
            session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
        )

    instances_by_status = {
        str(status): n
        for status, n in session.execute(
            select(Instance.status, func.count()).group_by(Instance.status)
        ).all()
    }

    revenue_cents = int(
        session.execute(
            select(func.coalesce(func.sum(Invoice.total_cents), 0)).where(
                Invoice.status == InvoiceStatus.PAID
            )
        ).scalar_one()
    )

    outstanding_cents = int(
        session.execute(
            select(
                func.coalesce(
                    func.sum(
                        Invoice.total_cents
                        - Invoice.amount_paid_cents
                        - Invoice.credit_applied_cents
                    ),
                    0,
                )
            ).where(Invoice.status == InvoiceStatus.OPEN)
        ).scalar_one()
    )

    return {
        "users": count(User),
        "accounts": count(Account),
        "instances_total": count(Instance, Instance.status != InstanceStatus.DELETED),
        "instances_by_status": instances_by_status,
        "instances_errored": count(Instance, Instance.status == InstanceStatus.ERROR),
        "jobs_queued": count(Job, Job.status == JobStatus.QUEUED),
        "jobs_running": count(Job, Job.status == JobStatus.RUNNING),
        "jobs_failed": count(Job, Job.status == JobStatus.FAILED),
        "subscriptions_active": count(
            Subscription, Subscription.status == SubscriptionStatus.ACTIVE
        ),
        "subscriptions_past_due": count(
            Subscription, Subscription.status == SubscriptionStatus.PAST_DUE
        ),
        "subscriptions_suspended": count(
            Subscription, Subscription.status == SubscriptionStatus.SUSPENDED
        ),
        "ips_available": count(IpAddress, IpAddress.status == IPStatus.AVAILABLE),
        "ips_assigned": count(IpAddress, IpAddress.status == IPStatus.ASSIGNED),
        "revenue_paid_cents": revenue_cents,
        "revenue_outstanding_cents": outstanding_cents,
    }


@router.get("/capacity")
def capacity(session: DbSession, user: StaffUser):
    """IP capacity per region and pool — the usual reason ordering starts failing."""
    out = []
    for region in session.execute(select(Region).order_by(Region.name)).scalars():
        pools = session.execute(select(IpPool).where(IpPool.region_id == region.id)).scalars().all()
        out.append(
            {
                "region": region.slug,
                "name": region.name,
                "available": ipam.region_available_count(session, region.id),
                "pools": [
                    {"name": p.name, "cidr": p.cidr, **ipam.pool_capacity(session, p.id)}
                    for p in pools
                ],
            }
        )
    return out


@router.get("/instances")
def all_instances(session: DbSession, user: StaffUser, status: str | None = None, limit: int = 100):
    stmt = select(Instance).order_by(Instance.created_at.desc()).limit(min(limit, 500))
    if status:
        stmt = stmt.where(Instance.status == status)

    return [
        {
            "id": i.id,
            "label": i.label,
            "hostname": i.hostname,
            "status": str(i.status),
            "account_id": i.account_id,
            "hypervisor_ref": i.hypervisor_ref,
            "primary_ip": i.primary_ip,
            "created_at": i.created_at,
            "drift_detected_at": i.drift_detected_at,
        }
        for i in session.execute(stmt).scalars()
    ]


@router.get("/jobs")
def all_jobs(session: DbSession, user: StaffUser, status: str | None = None, limit: int = 100):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))
    if status:
        stmt = stmt.where(Job.status == status)

    return [
        {
            "id": j.id,
            "type": str(j.type),
            "status": str(j.status),
            "instance_id": j.instance_id,
            "attempts": j.attempts,
            "error": j.error,
            "created_at": j.created_at,
            "finished_at": j.finished_at,
        }
        for j in session.execute(stmt).scalars()
    ]


@router.post("/instances/{instance_id}/reconcile")
def reconcile_one(instance_id: str, session: DbSession, user: AdminUser):
    """Force an immediate reconcile of one instance against the hypervisor."""
    from app.drivers import get_driver
    from app.services import provisioning

    instance = session.get(Instance, instance_id)
    if instance is None:
        return {"error": "not found"}
    return provisioning.reconcile_instance(session, instance, get_driver())


@router.get("/plans")
def list_all_plans(session: DbSession, user: StaffUser):
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "vcpu": p.vcpu,
            "ram_mb": p.ram_mb,
            "disk_gb": p.disk_gb,
            "price_cents": p.price_cents,
            "currency": p.currency,
            "is_active": p.is_active,
        }
        for p in session.execute(select(Plan).order_by(Plan.sort_order)).scalars()
    ]
