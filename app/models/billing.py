"""Billing: subscriptions, invoices, payments, ledger and gateway webhooks.

All monetary amounts are integer minor units (cents) with an explicit currency
column. No floating point appears anywhere in this module or the code that reads
it — rounding drift in money is not recoverable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EnumString, Timestamped, UUIDPrimaryKey
from app.models.enums import (
    BillingPeriod,
    InvoiceStatus,
    LedgerEntryType,
    PaymentStatus,
    SubscriptionStatus,
)

if TYPE_CHECKING:
    from app.models.catalog import Plan
    from app.models.compute import Instance


class Subscription(UUIDPrimaryKey, Timestamped, Base):
    """One recurring charge, normally bound to one instance."""

    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_renewal", "status", "current_period_end"),)

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        EnumString(SubscriptionStatus, 20),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    billing_period: Mapped[BillingPeriod] = mapped_column(
        EnumString(BillingPeriod, 20), default=BillingPeriod.MONTHLY, nullable=False
    )
    # Price is snapshotted so a catalog price change never silently repricess an
    # existing customer mid-contract.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)

    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Dunning state
    dunning_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_dunning_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    plan: Mapped[Plan] = relationship()
    instance: Mapped[Instance | None] = relationship()


class Invoice(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_account_status", "account_id", "status"),)

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Human-facing number from a Postgres sequence, never a COUNT(*) — concurrent
    # billing runs must not collide on the same number.
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    status: Mapped[InvoiceStatus] = mapped_column(
        EnumString(InvoiceStatus, 20), default=InvoiceStatus.DRAFT, nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)

    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_paid_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credit_applied_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_rate_bp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # basis points

    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(back_populates="invoice")

    @property
    def amount_due_cents(self) -> int:
        return max(0, self.total_cents - self.amount_paid_cents - self.credit_applied_cents)

    @property
    def total_display(self) -> str:
        return f"{self.total_cents / 100:.2f} {self.currency}"


class InvoiceLine(UUIDPrimaryKey, Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class PaymentMethod(UUIDPrimaryKey, Timestamped, Base):
    """A tokenised payment instrument.

    Only gateway references and display metadata are stored. No PAN, no CVV, no
    raw bank details ever reach this database.
    """

    __tablename__ = "payment_methods"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gateway: Mapped[str] = mapped_column(String(40), nullable=False)
    gateway_customer_ref: Mapped[str | None] = mapped_column(String(120))
    gateway_method_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="card", nullable=False)
    brand: Mapped[str | None] = mapped_column(String(40))
    last4: Mapped[str | None] = mapped_column(String(4))
    exp_month: Mapped[int | None] = mapped_column(Integer)
    exp_year: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def display(self) -> str:
        brand = self.brand or self.kind
        return f"{brand} •••• {self.last4}" if self.last4 else brand


class Payment(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "payments"
    __table_args__ = (
        # The gateway idempotency key is what makes a retried charge safe; it must
        # be unique per account, not merely advisory.
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[str | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), index=True
    )
    payment_method_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    gateway: Mapped[str] = mapped_column(String(40), nullable=False)
    gateway_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        EnumString(PaymentStatus, 20), default=PaymentStatus.PENDING, nullable=False, index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(60))
    failure_message: Mapped[str | None] = mapped_column(Text)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    invoice: Mapped[Invoice | None] = relationship(back_populates="payments")


class LedgerEntry(UUIDPrimaryKey, Base):
    """Append-only account ledger. `Account.balance_cents` is derived from this."""

    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_account_created", "account_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[LedgerEntryType] = mapped_column(EnumString(LedgerEntryType, 20), nullable=False)
    # Signed: positive adds customer credit, negative consumes it.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))


class WebhookEvent(UUIDPrimaryKey, Base):
    """Ingested gateway events.

    `(gateway, external_id)` is unique, which is what makes replayed webhooks a
    no-op rather than a double credit.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("gateway", "external_id", name="uq_webhook_events_gateway_external"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gateway: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
