"""Server-rendered customer portal.

Jinja2 + HTMX rather than a SPA: one deployable, no build pipeline, and the same
auth and ownership dependencies the JSON API uses.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.api.deps import (
    CurrentAccount,
    CurrentUser,
    DbSession,
    OptionalUser,
    client_ip,
    get_owned_instance,
)
from app.billing import get_gateway
from app.billing import subscriptions as subscription_service
from app.config import get_settings
from app.models.billing import Invoice, PaymentMethod, Subscription
from app.models.catalog import OsImage, Plan, Region
from app.models.compute import Instance, Job
from app.models.enums import InstanceStatus, JobType, SubscriptionStatus
from app.models.identity import SshKey
from app.portal.templating import issue_csrf_token, templates, verify_csrf
from app.security import ssh_key_fingerprint
from app.services import audit, provisioning
from app.services import auth as auth_service
from app.services.dispatch import enqueue_after_commit
from app.services.errors import DomainError, NotFoundError, ValidationError

router = APIRouter(include_in_schema=False)


def _ctx(request: Request, user=None, **extra) -> dict:
    settings = get_settings()
    marker = request.cookies.get(settings.session_cookie_name, "")
    return {
        "user": user,
        "csrf_token": issue_csrf_token(marker),
        "brand": "HostPlane",
        **extra,
    }


def _render(request: Request, template: str, user=None, status_code: int = 200, **extra):
    return templates.TemplateResponse(
        request, template, _ctx(request, user, **extra), status_code=status_code
    )


def _session_cookie(response: Response, user_id: str) -> None:
    from app.security import sign_session

    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        sign_session(user_id),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )


# --- public pages -----------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: DbSession, user: OptionalUser):
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)

    plans = (
        session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.price_cents)
        )
        .scalars()
        .all()
    )
    regions = (
        session.execute(select(Region).where(Region.is_active.is_(True)).order_by(Region.name))
        .scalars()
        .all()
    )
    return _render(request, "home.html", user, plans=plans, regions=regions)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    return _render(request, "login.html", None, next=next)


@router.post("/login")
def login_submit(
    request: Request,
    session: DbSession,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    settings = get_settings()
    ip = client_ip(request)

    try:
        auth_service.check_rate_limit(
            session,
            key=f"login:{ip}:{email.lower()}",
            limit=settings.login_rate_limit_per_minute,
            window_seconds=60,
        )
        user = auth_service.authenticate(session, email=email, password=password, source_ip=ip)
    except DomainError as exc:
        return _render(
            request, "login.html", None, status_code=exc.status_code, error=exc.message, next=next
        )

    # Only relative paths are honoured, so `?next=` cannot be used to bounce a
    # freshly authenticated user to an attacker's site.
    target = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    response = RedirectResponse(target, status_code=303)
    _session_cookie(response, user.id)
    return response


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return _render(request, "signup.html", None)


@router.post("/signup")
def signup_submit(
    request: Request,
    session: DbSession,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    settings = get_settings()
    ip = client_ip(request)

    try:
        auth_service.check_rate_limit(
            session,
            key=f"register:{ip}",
            limit=settings.signup_rate_limit_per_hour,
            window_seconds=3600,
        )
        user, _account = auth_service.register_user(
            session, email=email, password=password, full_name=full_name, source_ip=ip
        )
    except DomainError as exc:
        return _render(request, "signup.html", None, status_code=exc.status_code, error=exc.message)

    response = RedirectResponse("/dashboard", status_code=303)
    _session_cookie(response, user.id)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response


@router.get("/verify", response_class=HTMLResponse)
def verify_email_page(request: Request, session: DbSession, token: str = ""):
    try:
        auth_service.verify_email(session, token)
        message, ok = "Your email address is confirmed.", True
    except DomainError as exc:
        message, ok = exc.message, False
    return _render(request, "verify.html", None, message=message, ok=ok)


# --- authenticated pages ----------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, session: DbSession, user: CurrentUser, account: CurrentAccount):
    instances = (
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
    open_invoices = (
        session.execute(
            select(Invoice)
            .where(Invoice.account_id == account.id, Invoice.status == "open")
            .order_by(Invoice.due_at)
        )
        .scalars()
        .all()
    )
    return _render(
        request,
        "dashboard.html",
        user,
        account=account,
        instances=instances,
        open_invoices=open_invoices,
    )


@router.get("/order", response_class=HTMLResponse)
def order_page(request: Request, session: DbSession, user: CurrentUser, account: CurrentAccount):
    plans = (
        session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.price_cents)
        )
        .scalars()
        .all()
    )
    regions = (
        session.execute(select(Region).where(Region.is_active.is_(True)).order_by(Region.name))
        .scalars()
        .all()
    )
    images = (
        session.execute(
            select(OsImage)
            .where(OsImage.is_active.is_(True))
            .order_by(OsImage.sort_order, OsImage.name)
        )
        .scalars()
        .all()
    )
    keys = session.execute(select(SshKey).where(SshKey.account_id == account.id)).scalars().all()
    return _render(
        request,
        "order.html",
        user,
        account=account,
        plans=plans,
        regions=regions,
        images=images,
        ssh_keys=keys,
    )


@router.post("/order")
def order_submit(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
    label: str = Form(...),
    plan: str = Form(...),
    image: str = Form(...),
    region: str = Form(...),
    hostname: str = Form(""),
    ssh_key_id: str = Form(""),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)

    try:
        instance, job, root_password = provisioning.order_instance(
            session,
            account=account,
            plan_slug=plan,
            image_slug=image,
            region_slug=region,
            label=label,
            hostname=hostname or None,
            ssh_key_ids=[ssh_key_id] if ssh_key_id else [],
            actor_user_id=user.id,
        )
    except DomainError as exc:
        return order_page_with_error(request, session, user, account, exc)

    plan_row = session.get(Plan, instance.plan_id)
    if plan_row is not None:
        subscription_service.create_subscription(
            session, account_id=account.id, plan=plan_row, instance=instance
        )

    enqueue_after_commit(session, job.id)

    # The plaintext password exists only here. It is passed through a one-shot
    # redirect page rather than being stored, so it is shown exactly once.
    response = RedirectResponse(f"/instances/{instance.id}?new=1", status_code=303)
    response.set_cookie(
        "hostplane_new_password",
        root_password,
        max_age=300,
        httponly=True,
        samesite="lax",
        secure=get_settings().is_production,
        path="/",
    )
    return response


def order_page_with_error(request, session, user, account, exc: DomainError):
    plans = session.execute(select(Plan).where(Plan.is_active.is_(True))).scalars().all()
    regions = session.execute(select(Region).where(Region.is_active.is_(True))).scalars().all()
    images = session.execute(select(OsImage).where(OsImage.is_active.is_(True))).scalars().all()
    keys = session.execute(select(SshKey).where(SshKey.account_id == account.id)).scalars().all()
    return _render(
        request,
        "order.html",
        user,
        status_code=exc.status_code,
        error=exc.message,
        account=account,
        plans=plans,
        regions=regions,
        images=images,
        ssh_keys=keys,
    )


@router.get("/instances/{instance_id}", response_class=HTMLResponse)
def instance_detail(
    instance_id: str, request: Request, session: DbSession, user: CurrentUser, new: str = ""
):
    instance = get_owned_instance(instance_id, session, user)
    jobs = (
        session.execute(
            select(Job)
            .where(Job.instance_id == instance.id)
            .order_by(Job.created_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    plans = (
        session.execute(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order))
        .scalars()
        .all()
    )
    images = session.execute(select(OsImage).where(OsImage.is_active.is_(True))).scalars().all()

    # Read once and immediately clear, so a shared screen or a back button does
    # not re-reveal the password.
    root_password = request.cookies.get("hostplane_new_password") if new else None

    response = _render(
        request,
        "instance.html",
        user,
        instance=instance,
        jobs=jobs,
        plans=plans,
        images=images,
        root_password=root_password,
    )
    if root_password:
        response.delete_cookie("hostplane_new_password", path="/")
    return response


@router.get("/instances/{instance_id}/status", response_class=HTMLResponse)
def instance_status_fragment(
    instance_id: str, request: Request, session: DbSession, user: CurrentUser
):
    """HTMX polling target: re-renders just the status card while work is in flight."""
    instance = get_owned_instance(instance_id, session, user)
    jobs = (
        session.execute(
            select(Job)
            .where(Job.instance_id == instance.id)
            .order_by(Job.created_at.desc())
            .limit(5)
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "partials/instance_status.html",
        {"instance": instance, "jobs": jobs, "user": user},
    )


@router.post("/instances/{instance_id}/action")
def instance_action(
    instance_id: str,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    action: str = Form(...),
    plan: str = Form(""),
    image: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    instance = get_owned_instance(instance_id, session, user)

    try:
        job_type = JobType(action)
    except ValueError as exc:
        raise ValidationError(f"Unknown action '{action}'") from exc

    payload: dict = {}

    if job_type is JobType.RESIZE:
        target = session.execute(
            select(Plan).where(Plan.slug == plan, Plan.is_active.is_(True))
        ).scalar_one_or_none()
        if target is None:
            raise NotFoundError(f"Plan '{plan}' is not available")
        if target.disk_gb < instance.disk_gb:
            raise ValidationError("Disks cannot be shrunk, so this plan is not a valid target")
        payload["plan_id"] = target.id

    if job_type is JobType.REBUILD:
        if confirm != "yes":
            raise ValidationError("Confirm the rebuild — it erases every disk on this server")
        if image:
            target_image = session.execute(
                select(OsImage).where(OsImage.slug == image, OsImage.is_active.is_(True))
            ).scalar_one_or_none()
            if target_image is None:
                raise NotFoundError(f"Image '{image}' is not available")
            payload["image_id"] = target_image.id

    job = provisioning.request_action(
        session, instance=instance, job_type=job_type, payload=payload, actor_user_id=user.id
    )

    if job_type is JobType.RESIZE and payload.get("plan_id"):
        subscription = session.execute(
            select(Subscription).where(
                Subscription.instance_id == instance.id,
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
            )
        ).scalar_one_or_none()
        target = session.get(Plan, payload["plan_id"])
        if subscription is not None and target is not None:
            subscription_service.change_plan(session, subscription, target)

    if job_type is JobType.DELETE:
        subscription = session.execute(
            select(Subscription).where(
                Subscription.instance_id == instance.id,
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
            )
        ).scalar_one_or_none()
        if subscription is not None:
            subscription_service.cancel(session, subscription, immediate=True)

    enqueue_after_commit(session, job.id)
    return RedirectResponse(f"/instances/{instance.id}", status_code=303)


# --- billing pages ----------------------------------------------------------


@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, session: DbSession, user: CurrentUser, account: CurrentAccount):
    invoices = (
        session.execute(
            select(Invoice)
            .where(Invoice.account_id == account.id)
            .order_by(Invoice.created_at.desc())
        )
        .scalars()
        .all()
    )
    methods = (
        session.execute(
            select(PaymentMethod).where(
                PaymentMethod.account_id == account.id,
                PaymentMethod.detached_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    subs = (
        session.execute(select(Subscription).where(Subscription.account_id == account.id))
        .scalars()
        .all()
    )
    return _render(
        request,
        "billing.html",
        user,
        account=account,
        invoices=invoices,
        methods=methods,
        subscriptions=subs,
    )


@router.post("/billing/payment-methods")
def add_card(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
    token: str = Form(...),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    gateway = get_gateway()

    existing = (
        session.execute(
            select(PaymentMethod)
            .where(
                PaymentMethod.account_id == account.id,
                PaymentMethod.gateway_customer_ref.is_not(None),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    # `existing` is filtered on a non-null customer ref, but fall back to minting
    # one rather than passing None into the gateway if that ever changes.
    customer_ref = (existing.gateway_customer_ref if existing is not None else None) or (
        gateway.create_customer(account_id=account.id, email=account.billing_email).ref
    )
    method = gateway.attach_payment_method(customer_ref=customer_ref, method_token=token)

    for other in session.execute(
        select(PaymentMethod).where(
            PaymentMethod.account_id == account.id, PaymentMethod.is_default.is_(True)
        )
    ).scalars():
        other.is_default = False

    session.add(
        PaymentMethod(
            account_id=account.id,
            gateway=gateway.name,
            gateway_customer_ref=customer_ref,
            gateway_method_ref=method.ref,
            kind=method.kind,
            brand=method.brand,
            last4=method.last4,
            exp_month=method.exp_month,
            exp_year=method.exp_year,
            is_default=True,
        )
    )
    audit.record(
        session,
        action="payment_method.added",
        actor_user_id=user.id,
        account_id=account.id,
    )
    return RedirectResponse("/billing", status_code=303)


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(
    invoice_id: str,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.account_id != account.id:
        raise NotFoundError("Invoice not found")
    return _render(request, "invoice.html", user, account=account, invoice=invoice)


# --- account settings -------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: DbSession, user: CurrentUser, account: CurrentAccount):
    keys = (
        session.execute(
            select(SshKey).where(SshKey.account_id == account.id).order_by(SshKey.created_at)
        )
        .scalars()
        .all()
    )
    return _render(request, "settings.html", user, account=account, ssh_keys=keys)


@router.post("/settings/ssh-keys")
def add_ssh_key_form(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
    name: str = Form(...),
    public_key: str = Form(...),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    try:
        fingerprint = ssh_key_fingerprint(public_key)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    duplicate = session.execute(
        select(SshKey).where(SshKey.account_id == account.id, SshKey.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if duplicate is None:
        session.add(
            SshKey(
                account_id=account.id,
                name=name.strip()[:100],
                public_key=public_key.strip(),
                fingerprint=fingerprint,
            )
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/ssh-keys/{key_id}/delete")
def delete_ssh_key_form(
    key_id: str,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    key = session.get(SshKey, key_id)
    if key is not None and key.account_id == account.id:
        session.delete(key)
    return RedirectResponse("/settings", status_code=303)
