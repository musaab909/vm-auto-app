"""Staff and administrator portal pages.

Every route depends on `StaffUser`, so an ordinary customer session cannot reach
them even by guessing a URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from app.api.deps import DbSession, StaffUser
from app.models.billing import Invoice, Subscription
from app.models.catalog import OsImage, Plan, Region
from app.models.compute import Instance, Job
from app.models.enums import (
    InstanceStatus,
    InvoiceStatus,
    IPVersion,
    JobStatus,
    SubscriptionStatus,
)
from app.models.identity import Account, User
from app.models.network import IpPool
from app.portal.templating import issue_csrf_token, templates, verify_csrf
from app.services import ipam
from app.services.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/admin", include_in_schema=False)


def _render(request: Request, template: str, user, **extra):
    from app.config import get_settings

    marker = request.cookies.get(get_settings().session_cookie_name, "")
    return templates.TemplateResponse(
        request,
        f"admin/{template}",
        {
            "user": user,
            "csrf_token": issue_csrf_token(marker),
            "brand": "HostPlane",
            "admin": True,
            **extra,
        },
    )


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, session: DbSession, user: StaffUser):
    def count(model, *where) -> int:
        return int(
            session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()
        )

    paid_cents = int(
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

    stats = {
        "users": count(User),
        "accounts": count(Account),
        "instances": count(Instance, Instance.status != InstanceStatus.DELETED),
        "errored": count(Instance, Instance.status == InstanceStatus.ERROR),
        "jobs_failed": count(Job, Job.status == JobStatus.FAILED),
        "jobs_active": count(Job, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])),
        "past_due": count(Subscription, Subscription.status == SubscriptionStatus.PAST_DUE),
        "suspended": count(Subscription, Subscription.status == SubscriptionStatus.SUSPENDED),
        "paid_cents": paid_cents,
        "outstanding_cents": outstanding_cents,
    }

    regions = []
    for region in session.execute(select(Region).order_by(Region.name)).scalars():
        regions.append(
            {
                "region": region,
                "available": ipam.region_available_count(session, region.id),
                "pools": [
                    {"pool": p, **ipam.pool_capacity(session, p.id)}
                    for p in session.execute(
                        select(IpPool).where(IpPool.region_id == region.id)
                    ).scalars()
                ],
            }
        )

    recent_jobs = (
        session.execute(select(Job).order_by(Job.created_at.desc()).limit(15)).scalars().all()
    )

    return _render(
        request, "dashboard.html", user, stats=stats, regions=regions, recent_jobs=recent_jobs
    )


@router.get("/instances", response_class=HTMLResponse)
def admin_instances(request: Request, session: DbSession, user: StaffUser, status: str = ""):
    stmt = select(Instance).order_by(Instance.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Instance.status == status)

    return _render(
        request,
        "instances.html",
        user,
        instances=session.execute(stmt).scalars().all(),
        statuses=[s.value for s in InstanceStatus],
        active_status=status,
    )


@router.get("/jobs", response_class=HTMLResponse)
def admin_jobs(request: Request, session: DbSession, user: StaffUser, status: str = ""):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Job.status == status)

    return _render(
        request,
        "jobs.html",
        user,
        jobs=session.execute(stmt).scalars().all(),
        statuses=[s.value for s in JobStatus],
        active_status=status,
    )


@router.get("/accounts", response_class=HTMLResponse)
def admin_accounts(request: Request, session: DbSession, user: StaffUser):
    rows = []
    for account in session.execute(
        select(Account).order_by(Account.created_at.desc()).limit(200)
    ).scalars():
        rows.append(
            {
                "account": account,
                "instances": int(
                    session.execute(
                        select(func.count())
                        .select_from(Instance)
                        .where(
                            Instance.account_id == account.id,
                            Instance.status != InstanceStatus.DELETED,
                        )
                    ).scalar_one()
                ),
            }
        )
    return _render(request, "accounts.html", user, rows=rows)


@router.get("/invoices", response_class=HTMLResponse)
def admin_invoices(request: Request, session: DbSession, user: StaffUser):
    invoices = (
        session.execute(select(Invoice).order_by(Invoice.created_at.desc()).limit(200))
        .scalars()
        .all()
    )
    return _render(request, "invoices.html", user, invoices=invoices)


@router.get("/catalog", response_class=HTMLResponse)
def admin_catalog(request: Request, session: DbSession, user: StaffUser):
    return _render(
        request,
        "catalog.html",
        user,
        plans=session.execute(select(Plan).order_by(Plan.sort_order)).scalars().all(),
        images=session.execute(select(OsImage).order_by(OsImage.sort_order)).scalars().all(),
        regions=session.execute(select(Region).order_by(Region.name)).scalars().all(),
    )


@router.post("/catalog/plans/{plan_id}/toggle")
def toggle_plan(
    plan_id: str,
    request: Request,
    session: DbSession,
    user: StaffUser,
    csrf_token: str = Form(""),
):
    """Take a plan off sale without deleting it.

    Existing instances keep running: their spec and price were snapshotted at
    order time, so withdrawing a plan only stops new orders.
    """
    verify_csrf(request, csrf_token)
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")
    plan.is_active = not plan.is_active
    return RedirectResponse("/admin/catalog", status_code=303)


@router.get("/pools", response_class=HTMLResponse)
def admin_pools(request: Request, session: DbSession, user: StaffUser):
    pools = []
    for pool in session.execute(select(IpPool)).scalars():
        region = session.get(Region, pool.region_id)
        pools.append({"pool": pool, "region": region, **ipam.pool_capacity(session, pool.id)})

    return _render(
        request,
        "pools.html",
        user,
        pools=pools,
        regions=session.execute(select(Region).order_by(Region.name)).scalars().all(),
    )


@router.post("/pools")
def create_pool(
    request: Request,
    session: DbSession,
    user: StaffUser,
    region_id: str = Form(...),
    name: str = Form(...),
    cidr: str = Form(...),
    gateway: str = Form(""),
    vsphere_network: str = Form(""),
    vlan_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Create a pool and materialise its addresses."""
    verify_csrf(request, csrf_token)

    import ipaddress

    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ValidationError(f"'{cidr}' is not a valid CIDR block") from exc

    if session.get(Region, region_id) is None:
        raise NotFoundError("Region not found")

    pool = IpPool(
        region_id=region_id,
        name=name.strip()[:100],
        cidr=str(network),
        version=IPVersion.V4 if network.version == 4 else IPVersion.V6,
        gateway=gateway.strip() or None,
        prefix_length=network.prefixlen,
        vsphere_network=vsphere_network.strip() or None,
        vlan_id=int(vlan_id) if vlan_id.strip().isdigit() else None,
    )
    session.add(pool)
    session.flush()

    ipam.expand_pool(session, pool)
    return RedirectResponse("/admin/pools", status_code=303)


@router.post("/instances/{instance_id}/reconcile")
def reconcile_instance(
    instance_id: str,
    request: Request,
    session: DbSession,
    user: StaffUser,
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    from app.drivers import get_driver
    from app.services import provisioning

    instance = session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError("Instance not found")

    provisioning.reconcile_instance(session, instance, get_driver())
    return RedirectResponse("/admin/instances", status_code=303)
