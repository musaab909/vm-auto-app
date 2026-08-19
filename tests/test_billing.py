"""Billing tests: invoice arithmetic, renewals, dunning and the ledger."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.billing import dunning, invoicing, subscriptions
from app.billing.fake_gateway import FakeGateway
from app.billing.gateway import ChargeOutcome
from app.drivers.mock import MockDriver
from app.models.billing import Invoice, LedgerEntry, PaymentMethod
from app.models.compute import Job
from app.models.enums import (
    AccountStatus,
    InstanceStatus,
    InvoiceStatus,
    JobType,
    PaymentStatus,
    SubscriptionStatus,
)
from app.security import utcnow
from app.services import provisioning
from app.services.errors import ConflictError, PaymentError


def attach_card(db, account, token="tok_visa") -> PaymentMethod:
    gateway = FakeGateway()
    customer = gateway.create_customer(account_id=account.id, email=account.billing_email)
    method = gateway.attach_payment_method(customer_ref=customer.ref, method_token=token)
    record = PaymentMethod(
        account_id=account.id,
        gateway="fake",
        gateway_customer_ref=customer.ref,
        gateway_method_ref=method.ref,
        kind="card",
        brand=method.brand,
        last4=method.last4,
        is_default=True,
    )
    db.add(record)
    db.flush()
    return record


def make_subscription(db, account, plan, instance=None, start=None):
    return subscriptions.create_subscription(
        db, account_id=account.id, plan=plan, instance=instance, start=start
    )


# --- money arithmetic -------------------------------------------------------


@pytest.mark.parametrize(
    "subtotal,rate_bp,expected",
    [
        (1000, 0, 0),
        (1000, 1900, 190),  # 19% VAT
        (599, 1900, 114),  # 113.81 rounds half-up to 114
        (1, 1900, 0),  # 0.19 rounds down
        (3, 1900, 1),  # 0.57 rounds up
        (10000, 2100, 2100),
    ],
)
def test_tax_uses_integer_arithmetic(subtotal, rate_bp, expected):
    result = invoicing.apply_tax(subtotal, rate_bp)
    assert result == expected
    assert isinstance(result, int)


def test_invoice_totals_are_exact_integers(db, account, plan):
    invoice = invoicing.create_invoice(
        db,
        account=account,
        lines=[
            {"description": "VPS S", "unit_price_cents": 599, "quantity": 2},
            {"description": "Extra IP", "unit_price_cents": 150, "quantity": 1},
        ],
        tax_rate_bp=1900,
    )

    assert invoice.subtotal_cents == 599 * 2 + 150  # 1348
    assert invoice.tax_cents == 256  # 1348 * 0.19 = 256.12
    assert invoice.total_cents == 1604
    assert invoice.amount_due_cents == 1604


def test_invoice_numbers_come_from_a_sequence(db, account):
    first = invoicing.create_invoice(
        db, account=account, lines=[{"description": "a", "unit_price_cents": 100}]
    )
    second = invoicing.create_invoice(
        db, account=account, lines=[{"description": "b", "unit_price_cents": 100}]
    )

    assert first.number != second.number
    assert first.number.startswith("INV-")
    year = utcnow().year
    assert first.number == f"INV-{year}-000001"
    assert second.number == f"INV-{year}-000002"


def test_issuing_an_invoice_debits_the_ledger(db, account):
    assert account.balance_cents == 0

    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )

    assert account.balance_cents == -599
    entry = db.execute(select(LedgerEntry).where(LedgerEntry.invoice_id == invoice.id)).scalar_one()
    assert entry.amount_cents == -599
    assert entry.balance_after_cents == -599


# --- payment ----------------------------------------------------------------


def test_successful_charge_marks_invoice_paid(db, account, plan):
    attach_card(db, account)
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )

    payment = invoicing.charge_invoice(db, invoice=invoice, account=account, gateway=FakeGateway())

    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_due_cents == 0
    assert account.balance_cents == 0  # charge and payment net out


def test_declined_charge_is_recorded_not_raised(db, account):
    """A decline is a business outcome that drives dunning, not an exception."""
    attach_card(db, account, token="tok_decline")
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )

    payment = invoicing.charge_invoice(db, invoice=invoice, account=account, gateway=FakeGateway())

    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_code == "card_declined"
    assert invoice.status is InvoiceStatus.OPEN


def test_charge_without_a_payment_method_raises(db, account):
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )

    with pytest.raises(PaymentError, match="No payment method"):
        invoicing.charge_invoice(db, invoice=invoice, account=account, gateway=FakeGateway())


def test_worker_retry_after_a_lost_commit_does_not_double_charge(db, account):
    """The real double-charge risk: the gateway captured, then the worker died.

    On retry the invoice looks unpaid again, so `charge_invoice` calls the gateway
    a second time with the same idempotency key. The gateway must return the
    original charge rather than taking the money twice.
    """
    attach_card(db, account)
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )
    gateway = FakeGateway()

    first = invoicing.charge_invoice(
        db, invoice=invoice, account=account, gateway=gateway, attempt=1
    )
    assert first.status is PaymentStatus.SUCCEEDED
    captured_ref = first.gateway_ref

    # Simulate the crash. A rollback would have undone every write in that
    # transaction, so the ledger entry and the balance move must be reverted too,
    # not just the invoice — otherwise the retry settles from phantom credit.
    ledger_entry = db.execute(
        select(LedgerEntry).where(LedgerEntry.payment_id == first.id)
    ).scalar_one()
    db.delete(ledger_entry)
    account.balance_cents -= first.amount_cents
    invoice.status = InvoiceStatus.OPEN
    invoice.amount_paid_cents = 0
    invoice.paid_at = None
    first.status = PaymentStatus.PENDING
    first.gateway_ref = None
    db.flush()

    second = invoicing.charge_invoice(
        db, invoice=invoice, account=account, gateway=gateway, attempt=1
    )

    assert second.gateway_ref == captured_ref, "the gateway must replay, not re-capture"
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid_cents == 599, "the customer must be charged exactly once"


def test_paying_an_already_paid_invoice_is_rejected(db, account):
    attach_card(db, account)
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )
    invoicing.charge_invoice(db, invoice=invoice, account=account, gateway=FakeGateway())

    with pytest.raises(ConflictError, match="already paid"):
        invoicing.charge_invoice(
            db, invoice=invoice, account=account, gateway=FakeGateway(), attempt=2
        )


def test_account_credit_is_spent_before_the_card(db, account):
    invoicing.add_credit(db, account=account, amount_cents=1000, description="goodwill")
    assert account.balance_cents == 1000

    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )

    invoicing.charge_invoice(db, invoice=invoice, account=account, gateway=FakeGateway())

    assert invoice.status is InvoiceStatus.PAID
    assert invoice.credit_applied_cents == 599
    assert account.balance_cents == 401  # 1000 - 599, no card involved


def test_voiding_reverses_the_ledger(db, account):
    invoice = invoicing.create_invoice(
        db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
    )
    assert account.balance_cents == -599

    invoicing.void_invoice(db, invoice, account, reason="billed in error")

    assert invoice.status is InvoiceStatus.VOID
    assert account.balance_cents == 0


# --- proration --------------------------------------------------------------


def test_prorated_credit_halfway_through_a_period(db, account, plan):
    start = utcnow() - timedelta(days=15)
    subscription = make_subscription(db, account, plan, start=start)

    credit = subscriptions.prorated_credit_cents(subscription)

    # 15 of 30 days remain on a 599-cent plan.
    assert 290 <= credit <= 310


def test_prorated_credit_is_zero_after_the_period_ends(db, account, plan):
    subscription = make_subscription(db, account, plan, start=utcnow() - timedelta(days=31))
    assert subscriptions.prorated_credit_cents(subscription) == 0


def test_plan_change_returns_credit_and_charge(db, account, plan, bigger_plan):
    subscription = make_subscription(db, account, plan, start=utcnow() - timedelta(days=15))

    result = subscriptions.change_plan(db, subscription, bigger_plan)

    assert result["credit_cents"] > 0
    assert result["charge_cents"] > result["credit_cents"]  # upgrading costs more
    assert result["net_cents"] == result["charge_cents"] - result["credit_cents"]
    assert subscription.unit_price_cents == bigger_plan.price_cents


# --- renewal ----------------------------------------------------------------


def test_renewal_charges_and_rolls_the_period(db, account, plan):
    attach_card(db, account)
    start = utcnow() - timedelta(days=31)
    subscription = make_subscription(db, account, plan, start=start)
    original_end = subscription.current_period_end

    outcome = dunning.renew_subscription(db, subscription, FakeGateway())

    assert outcome.paid is True
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.current_period_end > original_end
    assert subscription.dunning_attempts == 0


def test_due_for_renewal_finds_only_expired_subscriptions(db, account, plan):
    expired = make_subscription(db, account, plan, start=utcnow() - timedelta(days=31))
    current = make_subscription(db, account, plan, start=utcnow())
    db.flush()

    due = subscriptions.due_for_renewal(db)

    assert expired in due
    assert current not in due


def test_cancelled_subscription_lapses_instead_of_renewing(db, catalog, account, plan):
    instance, job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label="web-01",
    )
    provisioning.run_job(db, job, MockDriver())

    subscription = make_subscription(
        db, account, plan, instance=instance, start=utcnow() - timedelta(days=31)
    )
    subscriptions.cancel(db, subscription, immediate=False)

    outcome = dunning.renew_subscription(db, subscription, FakeGateway())

    assert outcome.paid is False
    assert subscription.status is SubscriptionStatus.CANCELED

    # A delete job is queued so the server actually goes away.
    delete_jobs = (
        db.execute(select(Job).where(Job.instance_id == instance.id, Job.type == JobType.DELETE))
        .scalars()
        .all()
    )
    assert len(delete_jobs) == 1


# --- dunning ladder ---------------------------------------------------------


def test_failed_renewal_enters_dunning_and_schedules_a_retry(db, account, plan):
    attach_card(db, account, token="tok_decline")
    subscription = make_subscription(db, account, plan, start=utcnow() - timedelta(days=31))

    outcome = dunning.renew_subscription(db, subscription, FakeGateway())

    assert outcome.paid is False
    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert subscription.dunning_attempts == 1
    assert subscription.next_dunning_at is not None
    assert account.status is AccountStatus.DELINQUENT


def test_dunning_reuses_one_invoice_across_retries(db, account, plan):
    """Each retry must not raise a fresh invoice for the same period."""
    attach_card(db, account, token="tok_decline")
    subscription = make_subscription(db, account, plan, start=utcnow() - timedelta(days=31))
    gateway = FakeGateway()

    dunning.renew_subscription(db, subscription, gateway)
    dunning.renew_subscription(db, subscription, gateway)
    dunning.renew_subscription(db, subscription, gateway)

    invoices = db.execute(select(Invoice).where(Invoice.account_id == account.id)).scalars().all()
    assert len(invoices) == 1, "the customer must not be billed three times for one month"
    assert subscription.dunning_attempts == 3


def test_exhausted_retries_suspend_the_instance(db, catalog, account, plan):
    instance, job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label="web-01",
    )
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    attach_card(db, account, token="tok_decline")
    subscription = make_subscription(
        db, account, plan, instance=instance, start=utcnow() - timedelta(days=31)
    )
    gateway = FakeGateway()

    # Three configured retry days, so the fourth failure suspends.
    for _ in range(4):
        dunning.renew_subscription(db, subscription, gateway)

    assert subscription.status is SubscriptionStatus.SUSPENDED
    assert subscription.suspended_at is not None
    assert account.status is AccountStatus.SUSPENDED

    suspend_job = (
        db.execute(select(Job).where(Job.instance_id == instance.id, Job.type == JobType.SUSPEND))
        .scalars()
        .first()
    )
    assert suspend_job is not None

    # Suspension preserves the disk: the VM is powered off, not destroyed.
    provisioning.run_job(db, suspend_job, driver)
    assert instance.status is InstanceStatus.SUSPENDED
    assert driver.get_instance(instance.hypervisor_ref).exists is True


def test_suspension_grace_expiry_terminates_and_frees_the_ip(db, catalog, account, plan):
    instance, job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label="web-01",
    )
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    attach_card(db, account, token="tok_decline")
    subscription = make_subscription(
        db, account, plan, instance=instance, start=utcnow() - timedelta(days=31)
    )
    gateway = FakeGateway()

    for _ in range(4):
        dunning.renew_subscription(db, subscription, gateway)
    assert subscription.status is SubscriptionStatus.SUSPENDED

    # Advance past the termination grace window.
    subscription.next_dunning_at = utcnow() - timedelta(minutes=1)
    db.flush()

    outcomes = dunning.process_due_dunning(db, gateway)

    assert any(o.status is SubscriptionStatus.TERMINATED for o in outcomes)
    assert subscription.status is SubscriptionStatus.TERMINATED

    delete_job = (
        db.execute(select(Job).where(Job.instance_id == instance.id, Job.type == JobType.DELETE))
        .scalars()
        .first()
    )
    assert delete_job is not None

    provisioning.run_job(db, delete_job, driver)
    assert instance.status is InstanceStatus.DELETED

    from app.services import ipam

    assert ipam.region_available_count(db, catalog["region"].id) == 5


def test_paying_after_suspension_restores_service(db, catalog, account, plan):
    instance, job, _ = provisioning.order_instance(
        db,
        account=account,
        plan_slug="vps-s",
        image_slug="ubuntu-24-04",
        region_slug="eu-central",
        label="web-01",
    )
    driver = MockDriver()
    provisioning.run_job(db, job, driver)

    method = attach_card(db, account, token="tok_decline")
    subscription = make_subscription(
        db, account, plan, instance=instance, start=utcnow() - timedelta(days=31)
    )
    gateway = FakeGateway()

    for _ in range(4):
        dunning.renew_subscription(db, subscription, gateway)

    suspend_job = (
        db.execute(select(Job).where(Job.instance_id == instance.id, Job.type == JobType.SUSPEND))
        .scalars()
        .first()
    )
    provisioning.run_job(db, suspend_job, driver)
    assert instance.status is InstanceStatus.SUSPENDED

    # The customer updates their card and the next attempt succeeds.
    gateway_customer = method.gateway_customer_ref
    working = FakeGateway().attach_payment_method(
        customer_ref=gateway_customer, method_token="tok_visa"
    )
    method.gateway_method_ref = working.ref
    db.flush()

    outcome = dunning.renew_subscription(db, subscription, gateway)

    assert outcome.paid is True
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert account.status is AccountStatus.ACTIVE

    resume_job = (
        db.execute(select(Job).where(Job.instance_id == instance.id, Job.type == JobType.RESUME))
        .scalars()
        .first()
    )
    assert resume_job is not None

    provisioning.run_job(db, resume_job, driver)
    assert instance.status is InstanceStatus.RUNNING


def test_forced_decline_applies_to_every_card(db, account, plan):
    """The global override lets a demo or test flip the whole gateway to declining."""
    attach_card(db, account, token="tok_visa")
    FakeGateway.force_outcome = ChargeOutcome.FAILED
    try:
        invoice = invoicing.create_invoice(
            db, account=account, lines=[{"description": "VPS S", "unit_price_cents": 599}]
        )
        payment = invoicing.charge_invoice(
            db, invoice=invoice, account=account, gateway=FakeGateway()
        )
        assert payment.status is PaymentStatus.FAILED
    finally:
        FakeGateway.force_outcome = None
