"""Subscription lifecycle: creation, renewal and proration."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Subscription
from app.models.catalog import Plan
from app.models.compute import Instance
from app.models.enums import BillingPeriod, SubscriptionStatus
from app.security import utcnow
from app.services.errors import ValidationError

# Billing months are 30 days rather than calendar months. A fixed period keeps
# proration arithmetic exact and avoids the "billed on the 31st" class of bug;
# the trade-off is that renewal dates drift slowly against the calendar, which is
# normal for VPS hosting.
PERIOD_DAYS = {BillingPeriod.MONTHLY: 30, BillingPeriod.YEARLY: 365}


def period_length(period: BillingPeriod) -> timedelta:
    return timedelta(days=PERIOD_DAYS[period])


def advance_period(start: datetime, period: BillingPeriod) -> datetime:
    return start + period_length(period)


def create_subscription(
    session: Session,
    *,
    account_id: str,
    plan: Plan,
    instance: Instance | None = None,
    start: datetime | None = None,
) -> Subscription:
    start = start or utcnow()
    subscription = Subscription(
        account_id=account_id,
        instance_id=instance.id if instance else None,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        billing_period=plan.billing_period,
        # Snapshotted so a later catalog price change cannot silently reprice an
        # existing customer.
        unit_price_cents=plan.price_cents,
        currency=plan.currency,
        current_period_start=start,
        current_period_end=advance_period(start, plan.billing_period),
    )
    session.add(subscription)
    session.flush()
    return subscription


def due_for_renewal(session: Session, *, at: datetime | None = None) -> list[Subscription]:
    """Active subscriptions whose current period has ended."""
    at = at or utcnow()
    return list(
        session.execute(
            select(Subscription)
            .where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
                Subscription.current_period_end <= at,
                Subscription.terminated_at.is_(None),
            )
            .order_by(Subscription.current_period_end)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )


def roll_period(subscription: Subscription, *, at: datetime | None = None) -> None:
    """Advance to the next billing period after a successful payment."""
    at = at or utcnow()
    subscription.current_period_start = subscription.current_period_end
    subscription.current_period_end = advance_period(
        subscription.current_period_end, subscription.billing_period
    )
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.dunning_attempts = 0
    subscription.next_dunning_at = None
    subscription.past_due_since = None
    subscription.suspended_at = None


def cancel(
    session: Session, subscription: Subscription, *, immediate: bool = False
) -> Subscription:
    now = utcnow()
    if immediate:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = now
        subscription.current_period_end = now
    else:
        # The customer keeps what they paid for until the period they already
        # bought actually ends.
        subscription.cancel_at_period_end = True
        subscription.canceled_at = now
    session.flush()
    return subscription


def prorated_credit_cents(subscription: Subscription, *, at: datetime | None = None) -> int:
    """Unused value remaining in the current period, in cents.

    Truncates rather than rounds up, so the platform never credits more than was
    actually left unused.
    """
    at = at or utcnow()
    total = (subscription.current_period_end - subscription.current_period_start).total_seconds()
    if total <= 0:
        return 0
    remaining = (subscription.current_period_end - at).total_seconds()
    if remaining <= 0:
        return 0
    remaining = min(remaining, total)
    return int(subscription.unit_price_cents * remaining // total)


def prorated_charge_cents(
    new_plan: Plan, subscription: Subscription, *, at: datetime | None = None
) -> int:
    """Cost of the new plan for the remainder of the current period."""
    at = at or utcnow()
    total = (subscription.current_period_end - subscription.current_period_start).total_seconds()
    if total <= 0:
        return 0
    remaining = (subscription.current_period_end - at).total_seconds()
    if remaining <= 0:
        return 0
    remaining = min(remaining, total)
    return int(new_plan.price_cents * remaining // total)


def change_plan(
    session: Session, subscription: Subscription, new_plan: Plan, *, at: datetime | None = None
) -> dict[str, int]:
    """Switch plans mid-period.

    Returns the credit for the unused old plan and the charge for the new one, so
    the caller can put both on the customer's next invoice. Currencies must match;
    mixing them on one ledger has no correct answer.
    """
    at = at or utcnow()
    if new_plan.currency != subscription.currency:
        raise ValidationError(
            f"Cannot change from a {subscription.currency} plan to a {new_plan.currency} plan"
        )

    credit = prorated_credit_cents(subscription, at=at)
    charge = prorated_charge_cents(new_plan, subscription, at=at)

    subscription.plan_id = new_plan.id
    subscription.unit_price_cents = new_plan.price_cents
    session.flush()

    return {"credit_cents": credit, "charge_cents": charge, "net_cents": charge - credit}
