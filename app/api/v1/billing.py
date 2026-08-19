"""Billing routes: invoices, subscriptions, payment methods and the ledger."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.api.deps import CurrentAccount, CurrentUser, DbSession
from app.billing import get_gateway
from app.billing import invoicing as invoicing_service
from app.billing.gateway import GatewayError
from app.models.billing import (
    Invoice,
    LedgerEntry,
    PaymentMethod,
    Subscription,
)
from app.models.enums import InvoiceStatus
from app.schemas import (
    InvoiceOut,
    LedgerEntryOut,
    PaymentMethodCreate,
    PaymentMethodOut,
    SubscriptionOut,
)
from app.security import utcnow
from app.services import audit
from app.services.errors import ConflictError, NotFoundError, PaymentError

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(session: DbSession, account: CurrentAccount):
    return (
        session.execute(
            select(Invoice)
            .where(Invoice.account_id == account.id)
            .order_by(Invoice.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, session: DbSession, account: CurrentAccount):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.account_id != account.id:
        raise NotFoundError("Invoice not found")
    return invoice


@router.post("/invoices/{invoice_id}/pay", response_model=InvoiceOut)
def pay_invoice(invoice_id: str, session: DbSession, user: CurrentUser, account: CurrentAccount):
    """Retry payment on an open invoice on demand."""
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.account_id != account.id:
        raise NotFoundError("Invoice not found")
    if invoice.status is InvoiceStatus.PAID:
        raise ConflictError("This invoice is already paid")

    # A distinct attempt number keeps the gateway idempotency key unique, so a
    # manual retry is a genuinely new charge rather than a replay of the decline.
    payment = invoicing_service.charge_invoice(
        session,
        invoice=invoice,
        account=account,
        gateway=get_gateway(),
        attempt=int(utcnow().timestamp()),
    )

    audit.record(
        session,
        action="invoice.payment_attempted",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="invoice",
        target_id=invoice.id,
        status=str(payment.status),
    )

    if invoice.status is not InvoiceStatus.PAID:
        raise PaymentError(
            payment.failure_message or "The payment did not go through",
            details={"invoice": invoice.number, "status": str(payment.status)},
        )
    return invoice


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(session: DbSession, account: CurrentAccount):
    return (
        session.execute(
            select(Subscription)
            .where(Subscription.account_id == account.id)
            .order_by(Subscription.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.get("/payment-methods", response_model=list[PaymentMethodOut])
def list_payment_methods(session: DbSession, account: CurrentAccount):
    return (
        session.execute(
            select(PaymentMethod)
            .where(
                PaymentMethod.account_id == account.id,
                PaymentMethod.detached_at.is_(None),
            )
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post(
    "/payment-methods", response_model=PaymentMethodOut, status_code=status.HTTP_201_CREATED
)
def add_payment_method(
    payload: PaymentMethodCreate,
    session: DbSession,
    user: CurrentUser,
    account: CurrentAccount,
):
    """Attach a gateway-tokenised payment method.

    The API only ever sees a token. Raw card details never reach this service, so
    there is nothing here for a database compromise to expose.
    """
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

    try:
        # `existing` is filtered on a non-null customer ref, but fall back to
        # minting one rather than passing None into the gateway.
        customer_ref = (existing.gateway_customer_ref if existing is not None else None) or (
            gateway.create_customer(account_id=account.id, email=account.billing_email).ref
        )
        method = gateway.attach_payment_method(
            customer_ref=customer_ref, method_token=payload.token
        )
    except GatewayError as exc:
        raise PaymentError(f"The payment provider rejected that card: {exc.message}") from exc

    if payload.set_default:
        for other in session.execute(
            select(PaymentMethod).where(
                PaymentMethod.account_id == account.id, PaymentMethod.is_default.is_(True)
            )
        ).scalars():
            other.is_default = False

    record = PaymentMethod(
        account_id=account.id,
        gateway=gateway.name,
        gateway_customer_ref=customer_ref,
        gateway_method_ref=method.ref,
        kind=method.kind,
        brand=method.brand,
        last4=method.last4,
        exp_month=method.exp_month,
        exp_year=method.exp_year,
        is_default=payload.set_default,
    )
    session.add(record)
    session.flush()

    audit.record(
        session,
        action="payment_method.added",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="payment_method",
        target_id=record.id,
    )
    return record


@router.delete("/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_payment_method(
    method_id: str, session: DbSession, user: CurrentUser, account: CurrentAccount
):
    method = session.get(PaymentMethod, method_id)
    if method is None or method.account_id != account.id or method.detached_at is not None:
        raise NotFoundError("Payment method not found")

    get_gateway().detach_payment_method(method_ref=method.gateway_method_ref)
    method.detached_at = utcnow()
    method.is_default = False

    audit.record(
        session,
        action="payment_method.removed",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="payment_method",
        target_id=method.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ledger", response_model=list[LedgerEntryOut])
def list_ledger(session: DbSession, account: CurrentAccount, limit: int = 100):
    return (
        session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(min(limit, 500))
        )
        .scalars()
        .all()
    )
