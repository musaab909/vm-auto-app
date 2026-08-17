"""Gateway webhook ingestion.

Two properties matter here and are both enforced structurally rather than by
convention:

1. **Authenticity** — the signature is verified before the body is parsed, so an
   unsigned request never reaches business logic.
2. **Replay safety** — `(gateway, external_id)` is unique in the database, so a
   redelivered event is recorded and ignored rather than applied twice. Payment
   webhooks are retried aggressively by real gateways; without this, one retry
   would credit an invoice twice.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Header, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession
from app.billing import get_gateway
from app.billing import invoicing as invoicing_service
from app.billing.gateway import GatewayError
from app.models.billing import Invoice, Payment, WebhookEvent
from app.models.enums import PaymentStatus
from app.models.identity import Account
from app.security import utcnow
from app.services.errors import ValidationError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payments", status_code=status.HTTP_200_OK)
async def receive_payment_webhook(
    request: Request,
    session: DbSession,
    x_signature: str = Header(default=""),
):
    gateway = get_gateway()
    raw = await request.body()

    if not gateway.verify_webhook(payload=raw, signature=x_signature):
        # 400 rather than 401: this is a malformed request from a trusted origin,
        # and returning 401 invites gateways to retry against auth failures.
        raise ValidationError("Webhook signature verification failed")

    try:
        event = gateway.parse_webhook(payload=raw)
    except GatewayError as exc:
        raise ValidationError(exc.message) from exc

    record = WebhookEvent(
        created_at=utcnow(),
        gateway=gateway.name,
        external_id=event.external_id,
        event_type=event.type,
        payload=event.payload,
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        # Unique violation on (gateway, external_id) — this is a redelivery.
        session.rollback()
        log.info("webhook.duplicate", external_id=event.external_id, type=event.type)
        return {"status": "duplicate", "external_id": event.external_id}

    try:
        handled = _dispatch(session, event.type, event.payload)
        record.processed_at = utcnow()
    except Exception as exc:
        record.error = str(exc)[:2000]
        log.exception("webhook.failed", external_id=event.external_id, type=event.type)
        raise

    return {"status": "processed", "external_id": event.external_id, "handled": handled}


def _dispatch(session, event_type: str, payload: dict) -> bool:
    handler = _HANDLERS.get(event_type)
    if handler is None:
        log.info("webhook.ignored", type=event_type)
        return False
    handler(session, payload)
    return True


def _payment_succeeded(session, payload: dict) -> None:
    """Settle an invoice from an out-of-band success notification.

    Matched on the idempotency key rather than the charge ref, because the
    webhook may arrive before the synchronous charge call has returned and
    written `gateway_ref`.
    """
    key = payload.get("idempotency_key")
    if not key:
        return

    payment = session.execute(
        select(Payment).where(Payment.idempotency_key == key)
    ).scalar_one_or_none()
    if payment is None or payment.status is PaymentStatus.SUCCEEDED:
        return

    payment.status = PaymentStatus.SUCCEEDED
    payment.succeeded_at = utcnow()
    payment.gateway_ref = payload.get("charge_ref") or payment.gateway_ref

    invoice = session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    account = session.get(Account, payment.account_id)
    if invoice is not None and account is not None:
        # `register_payment` is itself idempotent on the ledger, so a webhook that
        # races the synchronous path cannot double-credit the account.
        invoicing_service.register_payment(
            session, invoice=invoice, account=account, payment=payment
        )


def _payment_failed(session, payload: dict) -> None:
    key = payload.get("idempotency_key")
    if not key:
        return

    payment = session.execute(
        select(Payment).where(Payment.idempotency_key == key)
    ).scalar_one_or_none()
    # A success already applied wins; a late failure notice must not undo it.
    if payment is None or payment.status is PaymentStatus.SUCCEEDED:
        return

    payment.status = PaymentStatus.FAILED
    payment.failure_code = payload.get("failure_code")


def _refund_succeeded(session, payload: dict) -> None:
    charge_ref = payload.get("charge_ref")
    amount = int(payload.get("amount_cents") or 0)
    if not charge_ref or amount <= 0:
        return

    payment = session.execute(
        select(Payment).where(Payment.gateway_ref == charge_ref)
    ).scalar_one_or_none()
    if payment is None:
        return

    payment.refunded_cents += amount
    if payment.refunded_cents >= payment.amount_cents:
        payment.status = PaymentStatus.REFUNDED


_HANDLERS = {
    "payment.succeeded": _payment_succeeded,
    "payment.failed": _payment_failed,
    "refund.succeeded": _refund_succeeded,
}
