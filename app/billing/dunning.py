"""The renewal and dunning state machine.

The escalation ladder, from a failed renewal payment to a terminated instance:

    ACTIVE
      └─ payment fails ─────────────→ PAST_DUE   (attempt 1)
           └─ retry on each configured dunning day
                └─ retries exhausted ─→ SUSPENDED (instance powered off, disk kept)
                     └─ grace expires ─→ TERMINATED (instance deleted, IP released)

Suspension is deliberately reversible and non-destructive: the disk survives, so
a customer who pays late gets their data back. Termination is the only step that
destroys anything, and it happens only after the full grace window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import invoicing, subscriptions
from app.billing.gateway import PaymentGateway
from app.config import get_settings
from app.models.billing import Invoice, Subscription
from app.models.compute import Instance, Job
from app.models.enums import (
    AccountStatus,
    InstanceStatus,
    InvoiceStatus,
    JobStatus,
    JobType,
    PaymentStatus,
    SubscriptionStatus,
)
from app.models.identity import Account
from app.security import utcnow
from app.services import audit
from app.services.auth import queue_email


@dataclass
class RenewalOutcome:
    subscription_id: str
    invoice_number: str | None
    paid: bool
    status: SubscriptionStatus
    detail: str


def renew_subscription(
    session: Session,
    subscription: Subscription,
    gateway: PaymentGateway,
    *,
    at: datetime | None = None,
) -> RenewalOutcome:
    """Invoice and charge one subscription for its next period."""
    at = at or utcnow()
    account = session.get(Account, subscription.account_id)
    if account is None:
        return RenewalOutcome(subscription.id, None, False, subscription.status, "account missing")

    # A subscription the customer already cancelled should lapse at period end,
    # not be billed again.
    if subscription.cancel_at_period_end:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.terminated_at = at
        session.flush()
        _queue_instance_delete(session, subscription, reason="subscription canceled")
        return RenewalOutcome(
            subscription.id, None, False, subscription.status, "canceled at period end"
        )

    invoice = _existing_open_invoice(session, subscription) or _issue_renewal_invoice(
        session, subscription, account, at=at
    )

    payment = invoicing.charge_invoice(
        session,
        invoice=invoice,
        account=account,
        gateway=gateway,
        attempt=subscription.dunning_attempts + 1,
    )

    if payment.status is PaymentStatus.SUCCEEDED or invoice.status is InvoiceStatus.PAID:
        subscriptions.roll_period(subscription, at=at)
        # Both states must clear. Only lifting DELINQUENT would leave a customer
        # who paid after suspension still locked out of ordering, since
        # `require_active_account` rejects SUSPENDED accounts.
        if account.status in {AccountStatus.DELINQUENT, AccountStatus.SUSPENDED}:
            account.status = AccountStatus.ACTIVE
            account.suspended_at = None
        _resume_instance_if_suspended(session, subscription)
        session.flush()
        audit.record(
            session,
            action="subscription.renewed",
            account_id=account.id,
            target_type="subscription",
            target_id=subscription.id,
            invoice=invoice.number,
        )
        return RenewalOutcome(subscription.id, invoice.number, True, subscription.status, "renewed")

    return _enter_dunning(session, subscription, account, invoice, at=at)


def _existing_open_invoice(session: Session, subscription: Subscription) -> Invoice | None:
    """Reuse the invoice already raised for this period.

    Without this, every dunning retry would raise a fresh invoice and the customer
    would be billed several times for one month.
    """
    return (
        session.execute(
            select(Invoice)
            .join(Invoice.lines)
            .where(
                Invoice.account_id == subscription.account_id,
                Invoice.status == InvoiceStatus.OPEN,
            )
            .order_by(Invoice.issued_at.desc())
        )
        .scalars()
        .first()
    )


def _issue_renewal_invoice(
    session: Session, subscription: Subscription, account: Account, *, at: datetime
) -> Invoice:
    period_start = subscription.current_period_end
    period_end = subscriptions.advance_period(period_start, subscription.billing_period)

    plan_name = subscription.plan.name if subscription.plan else "VPS plan"
    label = plan_name
    if subscription.instance_id:
        instance = session.get(Instance, subscription.instance_id)
        if instance is not None:
            label = f"{plan_name} — {instance.label}"

    return invoicing.create_invoice(
        session,
        account=account,
        lines=subscriptions_lines(subscription, period_start, period_end, label),
        issue=True,
    )


def subscriptions_lines(
    subscription: Subscription, period_start: datetime, period_end: datetime, label: str
) -> list[dict]:
    return invoicing.subscription_invoice_lines(
        subscription, period_start=period_start, period_end=period_end, label=label
    )


def _enter_dunning(
    session: Session,
    subscription: Subscription,
    account: Account,
    invoice: Invoice,
    *,
    at: datetime,
) -> RenewalOutcome:
    settings = get_settings()
    retry_days = settings.dunning_retry_days

    subscription.dunning_attempts += 1
    if subscription.past_due_since is None:
        subscription.past_due_since = at
    subscription.status = SubscriptionStatus.PAST_DUE
    account.status = AccountStatus.DELINQUENT

    if subscription.dunning_attempts <= len(retry_days):
        # Retries are scheduled from when the account first went past due, so the
        # ladder does not stretch when a retry runs late.
        offset = retry_days[subscription.dunning_attempts - 1]
        subscription.next_dunning_at = subscription.past_due_since + timedelta(days=offset)
        detail = f"payment failed, retry {subscription.dunning_attempts} scheduled"

        queue_email(
            session,
            to_email=account.billing_email or "",
            subject=f"Payment failed for invoice {invoice.number}",
            template="dunning_retry",
            body=(
                f"We could not take payment for invoice {invoice.number} "
                f"({invoice.total_display}).\n\n"
                f"We will try again on {subscription.next_dunning_at:%Y-%m-%d}. "
                "Update your payment method to avoid interruption.\n"
            ),
        )
    else:
        _suspend(session, subscription, account, invoice, at=at)
        detail = "retries exhausted, suspended"

    session.flush()
    audit.record(
        session,
        action="subscription.dunning",
        account_id=account.id,
        target_type="subscription",
        target_id=subscription.id,
        attempt=subscription.dunning_attempts,
        invoice=invoice.number,
    )
    return RenewalOutcome(subscription.id, invoice.number, False, subscription.status, detail)


def _suspend(
    session: Session,
    subscription: Subscription,
    account: Account,
    invoice: Invoice,
    *,
    at: datetime,
) -> None:
    """Power the instance off but keep its disk, so paying up restores service."""
    settings = get_settings()

    subscription.status = SubscriptionStatus.SUSPENDED
    subscription.suspended_at = at
    subscription.next_dunning_at = at + timedelta(days=settings.terminate_after_suspend_days)
    account.status = AccountStatus.SUSPENDED
    account.suspended_at = at

    _queue_instance_job(session, subscription, JobType.SUSPEND, reason="non-payment")

    queue_email(
        session,
        to_email=account.billing_email or "",
        subject="Your service has been suspended",
        template="dunning_suspend",
        body=(
            f"Invoice {invoice.number} ({invoice.total_display}) remains unpaid, so your "
            "service has been suspended.\n\n"
            "Your data is intact. Settle the invoice to restore service within "
            f"{settings.terminate_after_suspend_days} days, after which the server and "
            "its data will be deleted permanently.\n"
        ),
    )


def process_due_dunning(
    session: Session, gateway: PaymentGateway, *, at: datetime | None = None
) -> list[RenewalOutcome]:
    """Run every dunning action that has come due."""
    at = at or utcnow()
    outcomes: list[RenewalOutcome] = []

    due = (
        session.execute(
            select(Subscription)
            .where(
                Subscription.status.in_(
                    [SubscriptionStatus.PAST_DUE, SubscriptionStatus.SUSPENDED]
                ),
                Subscription.next_dunning_at.is_not(None),
                Subscription.next_dunning_at <= at,
                Subscription.terminated_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    for subscription in due:
        if subscription.status is SubscriptionStatus.SUSPENDED:
            outcomes.append(_terminate(session, subscription, at=at))
        else:
            outcomes.append(renew_subscription(session, subscription, gateway, at=at))

    return outcomes


def _terminate(session: Session, subscription: Subscription, *, at: datetime) -> RenewalOutcome:
    """Delete the instance after the suspension grace window has expired."""
    account = session.get(Account, subscription.account_id)

    subscription.status = SubscriptionStatus.TERMINATED
    subscription.terminated_at = at
    subscription.next_dunning_at = None

    _queue_instance_delete(session, subscription, reason="non-payment")

    if account is not None:
        _mark_uncollectible(session, account)
        queue_email(
            session,
            to_email=account.billing_email or "",
            subject="Your service has been terminated",
            template="dunning_terminate",
            body=(
                "Your unpaid service has been terminated and the server deleted. "
                "This cannot be undone.\n"
            ),
        )
        audit.record(
            session,
            action="subscription.terminated",
            account_id=account.id,
            target_type="subscription",
            target_id=subscription.id,
            reason="non-payment",
        )

    session.flush()
    return RenewalOutcome(
        subscription.id, None, False, subscription.status, "terminated after grace period"
    )


def _mark_uncollectible(session: Session, account: Account) -> None:
    for invoice in invoicing.outstanding_invoices(session, account.id):
        invoice.status = InvoiceStatus.UNCOLLECTIBLE
    session.flush()


def _queue_instance_job(
    session: Session, subscription: Subscription, job_type: JobType, *, reason: str
) -> Job | None:
    """Queue a lifecycle job for a subscription's instance.

    Billing bypasses `provisioning.request_action` on purpose: suspension and
    termination must proceed regardless of the instance's current state, and are
    not subject to the customer-facing transition rules.
    """
    if not subscription.instance_id:
        return None

    instance = session.get(Instance, subscription.instance_id)
    if instance is None or instance.status in {InstanceStatus.DELETED}:
        return None

    job = Job(
        instance_id=instance.id,
        account_id=subscription.account_id,
        type=job_type,
        status=JobStatus.QUEUED,
        payload={"reason": reason, "system": True},
    )
    session.add(job)
    session.flush()
    return job


def _queue_instance_delete(
    session: Session, subscription: Subscription, *, reason: str
) -> Job | None:
    return _queue_instance_job(session, subscription, JobType.DELETE, reason=reason)


def _resume_instance_if_suspended(session: Session, subscription: Subscription) -> None:
    if not subscription.instance_id:
        return
    instance = session.get(Instance, subscription.instance_id)
    if instance is not None and instance.status is InstanceStatus.SUSPENDED:
        _queue_instance_job(session, subscription, JobType.RESUME, reason="payment received")
