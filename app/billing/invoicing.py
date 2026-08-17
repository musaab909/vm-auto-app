"""Invoice construction, payment application and the account ledger.

Every amount here is an integer number of minor units. Tax is expressed in basis
points and applied with integer arithmetic, so no rounding drift can accumulate
across a customer's billing history.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.billing.gateway import ChargeOutcome, GatewayError, PaymentGateway
from app.config import get_settings
from app.models.billing import (
    Invoice,
    InvoiceLine,
    LedgerEntry,
    Payment,
    PaymentMethod,
    Subscription,
)
from app.models.enums import InvoiceStatus, LedgerEntryType, PaymentStatus
from app.models.identity import Account
from app.security import utcnow
from app.services.errors import ConflictError, PaymentError, ValidationError

INVOICE_SEQUENCE = "invoice_number_seq"


def apply_tax(subtotal_cents: int, tax_rate_bp: int) -> int:
    """Tax on a subtotal, in cents, rounded half-up with integer maths only."""
    if tax_rate_bp <= 0:
        return 0
    return (subtotal_cents * tax_rate_bp + 5000) // 10000


def next_invoice_number(session: Session) -> str:
    """Allocate an invoice number from a Postgres sequence.

    A sequence rather than `COUNT(*) + 1`: concurrent billing runs must never
    produce the same number, and sequence values survive rollback without reuse.
    """
    year = utcnow().year
    value = session.execute(select(func.nextval(text(f"'{INVOICE_SEQUENCE}'")))).scalar_one()
    return f"INV-{year}-{int(value):06d}"


def create_invoice(
    session: Session,
    *,
    account: Account,
    lines: list[dict],
    tax_rate_bp: int = 0,
    due_days: int | None = None,
    issue: bool = True,
    notes: str | None = None,
) -> Invoice:
    """Build an invoice from line dicts.

    Each line needs `description`, `unit_price_cents` and optionally `quantity`,
    `subscription_id`, `period_start`, `period_end`.
    """
    if not lines:
        raise ValidationError("An invoice needs at least one line")

    settings = get_settings()
    now = utcnow()

    invoice = Invoice(
        account_id=account.id,
        number=next_invoice_number(session),
        status=InvoiceStatus.OPEN if issue else InvoiceStatus.DRAFT,
        currency=account.currency,
        tax_rate_bp=tax_rate_bp,
        issued_at=now if issue else None,
        due_at=now + timedelta(days=due_days if due_days is not None else settings.invoice_due_days)
        if issue
        else None,
        notes=notes,
    )
    session.add(invoice)
    session.flush()

    subtotal = 0
    for index, line in enumerate(lines):
        quantity = int(line.get("quantity", 1))
        unit_price = int(line["unit_price_cents"])
        amount = quantity * unit_price
        subtotal += amount
        session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=line.get("subscription_id"),
                description=str(line["description"])[:300],
                quantity=quantity,
                unit_price_cents=unit_price,
                amount_cents=amount,
                period_start=line.get("period_start"),
                period_end=line.get("period_end"),
                sort_order=index,
            )
        )

    invoice.subtotal_cents = subtotal
    invoice.tax_cents = apply_tax(subtotal, tax_rate_bp)
    invoice.total_cents = subtotal + invoice.tax_cents
    session.flush()

    if issue:
        _record_ledger(
            session,
            account=account,
            entry_type=LedgerEntryType.CHARGE,
            amount_cents=-invoice.total_cents,
            description=f"Invoice {invoice.number}",
            invoice_id=invoice.id,
        )

    return invoice


def subscription_invoice_lines(
    subscription: Subscription, *, period_start: datetime, period_end: datetime, label: str
) -> list[dict]:
    return [
        {
            "description": f"{label} ({period_start:%Y-%m-%d} to {period_end:%Y-%m-%d})",
            "quantity": 1,
            "unit_price_cents": subscription.unit_price_cents,
            "subscription_id": subscription.id,
            "period_start": period_start,
            "period_end": period_end,
        }
    ]


# --- payment application ----------------------------------------------------


def available_credit_cents(invoice: Invoice, account: Account) -> int:
    """Credit the account can spend on `invoice`, in cents.

    Issuing the invoice already debited `balance_cents` by its total, so the raw
    balance understates what is spendable by exactly this invoice's outstanding
    amount. Adding that back recovers the customer's position immediately before
    the invoice — without it, a customer holding 1000 of credit against a 599
    invoice would see only 401 applied and be charged the remaining 198 to a card
    they should not have needed.
    """
    return max(0, account.balance_cents + invoice.amount_due_cents)


def apply_account_credit(session: Session, invoice: Invoice, account: Account) -> int:
    """Spend available account credit against an invoice. Returns cents applied.

    Deliberately records no ledger entry: the invoice's own CHARGE entry already
    moved the balance, so writing a second entry here would debit the customer
    twice for one invoice.
    """
    if invoice.amount_due_cents <= 0:
        return 0

    applied = min(available_credit_cents(invoice, account), invoice.amount_due_cents)
    if applied <= 0:
        return 0

    invoice.credit_applied_cents += applied

    if invoice.amount_due_cents == 0:
        mark_paid(session, invoice)

    session.flush()
    return applied


def charge_invoice(
    session: Session,
    *,
    invoice: Invoice,
    account: Account,
    gateway: PaymentGateway,
    method: PaymentMethod | None = None,
    attempt: int = 1,
) -> Payment:
    """Charge an invoice's outstanding balance to a payment method.

    A decline is recorded as a failed `Payment` and returned, not raised —
    declines are ordinary business events that drive dunning. Only a broken
    gateway raises.
    """
    if invoice.status is InvoiceStatus.PAID:
        raise ConflictError(f"Invoice {invoice.number} is already paid")
    if invoice.status in {InvoiceStatus.VOID, InvoiceStatus.UNCOLLECTIBLE}:
        raise ConflictError(f"Invoice {invoice.number} is {invoice.status}")

    apply_account_credit(session, invoice, account)
    if invoice.amount_due_cents == 0:
        return _synthetic_credit_payment(session, invoice, account)

    method = method or default_payment_method(session, account.id)
    if method is None:
        raise PaymentError(
            "No payment method is on file for this account",
            details={"invoice": invoice.number},
        )

    # Deterministic and unique per (invoice, attempt): a worker retry reuses the
    # same key and the gateway returns the original result rather than charging
    # the customer twice.
    idempotency_key = f"inv_{invoice.id}_att_{attempt}"

    existing = session.execute(
        select(Payment).where(Payment.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None and existing.status is PaymentStatus.SUCCEEDED:
        return existing

    payment = existing or Payment(
        account_id=account.id,
        invoice_id=invoice.id,
        payment_method_id=method.id,
        gateway=gateway.name,
        idempotency_key=idempotency_key,
        amount_cents=invoice.amount_due_cents,
        currency=invoice.currency,
        status=PaymentStatus.PENDING,
    )
    if existing is None:
        session.add(payment)
        session.flush()

    try:
        result = gateway.charge(
            amount_cents=invoice.amount_due_cents,
            currency=invoice.currency,
            method_ref=method.gateway_method_ref,
            customer_ref=method.gateway_customer_ref,
            idempotency_key=idempotency_key,
            description=f"Invoice {invoice.number}",
        )
    except GatewayError as exc:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = "gateway_error"
        payment.failure_message = exc.message
        session.flush()
        return payment

    payment.gateway_ref = result.ref

    if result.outcome is ChargeOutcome.SUCCEEDED:
        payment.status = PaymentStatus.SUCCEEDED
        payment.succeeded_at = utcnow()
        register_payment(session, invoice=invoice, account=account, payment=payment)
    elif result.outcome is ChargeOutcome.REQUIRES_ACTION:
        payment.status = PaymentStatus.REQUIRES_ACTION
        payment.failure_message = result.failure_message
    else:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = result.failure_code
        payment.failure_message = result.failure_message

    session.flush()
    return payment


def register_payment(
    session: Session, *, invoice: Invoice, account: Account, payment: Payment
) -> None:
    """Apply a successful payment to its invoice and the ledger.

    Idempotent on the ledger: a webhook arriving after the synchronous charge
    already settled the invoice must not credit the account twice.
    """
    already_recorded = session.execute(
        select(LedgerEntry).where(LedgerEntry.payment_id == payment.id)
    ).scalar_one_or_none()
    if already_recorded is not None:
        return

    invoice.amount_paid_cents += payment.amount_cents

    _record_ledger(
        session,
        account=account,
        entry_type=LedgerEntryType.PAYMENT,
        amount_cents=payment.amount_cents,
        description=f"Payment for invoice {invoice.number}",
        invoice_id=invoice.id,
        payment_id=payment.id,
    )

    if invoice.amount_due_cents == 0:
        mark_paid(session, invoice)

    session.flush()


def mark_paid(session: Session, invoice: Invoice) -> None:
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = utcnow()
    session.flush()


def void_invoice(session: Session, invoice: Invoice, account: Account, reason: str) -> None:
    if invoice.status is InvoiceStatus.PAID:
        raise ConflictError("A paid invoice cannot be voided; issue a refund instead")

    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = utcnow()
    invoice.notes = ((invoice.notes or "") + f"\nVoided: {reason}").strip()

    # Reverse the original charge so the ledger nets to zero for this invoice.
    _record_ledger(
        session,
        account=account,
        entry_type=LedgerEntryType.ADJUSTMENT,
        amount_cents=invoice.total_cents,
        description=f"Voided invoice {invoice.number}",
        invoice_id=invoice.id,
    )
    session.flush()


def add_credit(
    session: Session, *, account: Account, amount_cents: int, description: str
) -> LedgerEntry:
    if amount_cents <= 0:
        raise ValidationError("Credit must be a positive amount")
    return _record_ledger(
        session,
        account=account,
        entry_type=LedgerEntryType.CREDIT,
        amount_cents=amount_cents,
        description=description,
    )


def default_payment_method(session: Session, account_id: str) -> PaymentMethod | None:
    return (
        session.execute(
            select(PaymentMethod)
            .where(
                PaymentMethod.account_id == account_id,
                PaymentMethod.detached_at.is_(None),
            )
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc())
        )
        .scalars()
        .first()
    )


def outstanding_invoices(session: Session, account_id: str) -> list[Invoice]:
    return list(
        session.execute(
            select(Invoice)
            .where(Invoice.account_id == account_id, Invoice.status == InvoiceStatus.OPEN)
            .order_by(Invoice.issued_at)
        )
        .scalars()
        .all()
    )


def _record_ledger(
    session: Session,
    *,
    account: Account,
    entry_type: LedgerEntryType,
    amount_cents: int,
    description: str,
    invoice_id: str | None = None,
    payment_id: str | None = None,
) -> LedgerEntry:
    """Append a ledger entry and move the account's denormalised balance with it.

    The two always change together so `Account.balance_cents` can never drift from
    the sum of the ledger.
    """
    account.balance_cents += amount_cents
    entry = LedgerEntry(
        created_at=utcnow(),
        account_id=account.id,
        type=entry_type,
        amount_cents=amount_cents,
        balance_after_cents=account.balance_cents,
        currency=account.currency,
        description=description[:300],
        invoice_id=invoice_id,
        payment_id=payment_id,
    )
    session.add(entry)
    session.flush()
    return entry


def _synthetic_credit_payment(session: Session, invoice: Invoice, account: Account) -> Payment:
    """Represent a fully credit-settled invoice as a zero-value payment row.

    Keeps the invoice→payment relationship uniform, so reporting does not need a
    special case for invoices that never touched the gateway.
    """
    payment = Payment(
        account_id=account.id,
        invoice_id=invoice.id,
        gateway="credit",
        idempotency_key=f"credit_{invoice.id}",
        amount_cents=0,
        currency=invoice.currency,
        status=PaymentStatus.SUCCEEDED,
        succeeded_at=utcnow(),
    )
    session.add(payment)
    session.flush()
    return payment
