"""Deterministic in-memory payment gateway.

No live payment provider was in scope, so this is the gateway the platform runs
against. It is not a stub that always says yes: it declines on demand, enforces
idempotency the way a real gateway does, and emits signed webhook events, so the
decline path, the retry path and the webhook path are all genuinely exercised
rather than being untested code that first runs in production.

Card behaviour is driven by the token suffix, mirroring how real gateways expose
test cards:

  * ``tok_decline``          → declined
  * ``tok_insufficient``     → declined, insufficient funds
  * ``tok_action``           → requires 3-D Secure
  * ``tok_error``            → transport failure (retryable `GatewayError`)
  * anything else            → succeeds
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field

from app.billing.gateway import (
    ChargeOutcome,
    ChargeResult,
    GatewayCustomer,
    GatewayError,
    GatewayEvent,
    GatewayMethod,
    RefundResult,
)
from app.config import get_settings
from app.security import sign_webhook, verify_webhook_signature


@dataclass
class _FakeCharge:
    ref: str
    amount_cents: int
    currency: str
    outcome: ChargeOutcome
    refunded_cents: int = 0
    idempotency_key: str | None = None


@dataclass
class _FakeMethod:
    ref: str
    customer_ref: str
    token: str
    brand: str = "visa"
    last4: str = "4242"
    exp_month: int = 12
    exp_year: int = 2030
    detached: bool = False


@dataclass
class _FakeCustomer:
    ref: str
    account_id: str
    email: str | None = None
    methods: list[str] = field(default_factory=list)


class FakeGateway:
    """A payment gateway backed by process-local dicts."""

    name = "fake"

    _customers: dict[str, _FakeCustomer] = {}
    _methods: dict[str, _FakeMethod] = {}
    _charges: dict[str, _FakeCharge] = {}
    # idempotency_key -> charge ref, so a retried charge returns the original
    # result instead of taking the customer's money twice.
    _idempotency: dict[str, str] = {}
    _events: list[GatewayEvent] = []
    _lock = threading.RLock()

    # Global override used by tests and demos to force every charge to decline.
    force_outcome: ChargeOutcome | None = None

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._customers.clear()
            cls._methods.clear()
            cls._charges.clear()
            cls._idempotency.clear()
            cls._events.clear()
            cls.force_outcome = None

    @classmethod
    def emitted_events(cls) -> list[GatewayEvent]:
        with cls._lock:
            return list(cls._events)

    # --- gateway contract ---------------------------------------------------

    def create_customer(self, *, account_id: str, email: str | None) -> GatewayCustomer:
        ref = f"cus_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._customers[ref] = _FakeCustomer(ref=ref, account_id=account_id, email=email)
        return GatewayCustomer(ref=ref, email=email)

    def attach_payment_method(self, *, customer_ref: str, method_token: str) -> GatewayMethod:
        with self._lock:
            customer = self._customers.get(customer_ref)
            if customer is None:
                raise GatewayError(f"Unknown customer {customer_ref}", retryable=False)

            if method_token.endswith("error"):
                raise GatewayError("Gateway unavailable while attaching card")

            ref = f"pm_{uuid.uuid4().hex[:16]}"
            brand, last4 = self._card_display(method_token)
            self._methods[ref] = _FakeMethod(
                ref=ref, customer_ref=customer_ref, token=method_token, brand=brand, last4=last4
            )
            customer.methods.append(ref)

        return GatewayMethod(
            ref=ref, kind="card", brand=brand, last4=last4, exp_month=12, exp_year=2030
        )

    def detach_payment_method(self, *, method_ref: str) -> None:
        with self._lock:
            method = self._methods.get(method_ref)
            if method is not None:
                method.detached = True

    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        method_ref: str,
        customer_ref: str | None,
        idempotency_key: str,
        description: str | None = None,
    ) -> ChargeResult:
        if amount_cents <= 0:
            raise GatewayError("Charge amount must be positive", retryable=False)

        with self._lock:
            # Replaying an idempotency key returns the original outcome, exactly as
            # a real gateway does — this is what makes worker retries safe.
            existing_ref = self._idempotency.get(idempotency_key)
            if existing_ref is not None:
                charge = self._charges[existing_ref]
                return ChargeResult(
                    outcome=charge.outcome,
                    ref=charge.ref,
                    amount_cents=charge.amount_cents,
                    currency=charge.currency,
                )

            method = self._methods.get(method_ref)
            if method is None or method.detached:
                return ChargeResult(
                    outcome=ChargeOutcome.FAILED,
                    amount_cents=amount_cents,
                    currency=currency,
                    failure_code="method_not_found",
                    failure_message="Payment method is no longer valid",
                )

            outcome, code, message = self._decide(method.token)

            ref = f"ch_{uuid.uuid4().hex[:16]}"
            self._charges[ref] = _FakeCharge(
                ref=ref,
                amount_cents=amount_cents,
                currency=currency,
                outcome=outcome,
                idempotency_key=idempotency_key,
            )
            self._idempotency[idempotency_key] = ref

            self._emit(
                "payment.succeeded" if outcome is ChargeOutcome.SUCCEEDED else "payment.failed",
                {
                    "charge_ref": ref,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "idempotency_key": idempotency_key,
                    "failure_code": code,
                },
            )

        return ChargeResult(
            outcome=outcome,
            ref=ref,
            amount_cents=amount_cents,
            currency=currency,
            failure_code=code,
            failure_message=message,
            action_url=(
                f"https://fake-gateway.local/3ds/{ref}"
                if outcome is ChargeOutcome.REQUIRES_ACTION
                else None
            ),
        )

    def refund(self, *, charge_ref: str, amount_cents: int, idempotency_key: str) -> RefundResult:
        with self._lock:
            charge = self._charges.get(charge_ref)
            if charge is None:
                return RefundResult(
                    ref=None,
                    amount_cents=0,
                    succeeded=False,
                    failure_message=f"Unknown charge {charge_ref}",
                )
            refundable = charge.amount_cents - charge.refunded_cents
            if amount_cents > refundable:
                return RefundResult(
                    ref=None,
                    amount_cents=0,
                    succeeded=False,
                    failure_message=(f"Cannot refund {amount_cents}; only {refundable} remains"),
                )
            charge.refunded_cents += amount_cents
            ref = f"re_{uuid.uuid4().hex[:16]}"
            self._emit(
                "refund.succeeded",
                {"refund_ref": ref, "charge_ref": charge_ref, "amount_cents": amount_cents},
            )
        return RefundResult(ref=ref, amount_cents=amount_cents, succeeded=True)

    def verify_webhook(self, *, payload: bytes, signature: str) -> bool:
        return verify_webhook_signature(payload, signature, get_settings().gateway_webhook_secret)

    def parse_webhook(self, *, payload: bytes) -> GatewayEvent:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GatewayError("Webhook body is not valid JSON", retryable=False) from exc

        external_id = data.get("id")
        event_type = data.get("type")
        if not external_id or not event_type:
            raise GatewayError("Webhook is missing 'id' or 'type'", retryable=False)

        return GatewayEvent(
            external_id=str(external_id), type=str(event_type), payload=data.get("data") or {}
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def sign(payload: bytes) -> str:
        """Sign a payload the way the gateway would, for tests and local replay."""
        return sign_webhook(payload, get_settings().gateway_webhook_secret)

    def _decide(self, token: str) -> tuple[ChargeOutcome, str | None, str | None]:
        forced = type(self).force_outcome
        if forced is not None:
            if forced is ChargeOutcome.FAILED:
                return forced, "card_declined", "Your card was declined"
            return forced, None, None

        if token.endswith("error"):
            raise GatewayError("Gateway timed out")
        if token.endswith("decline"):
            return ChargeOutcome.FAILED, "card_declined", "Your card was declined"
        if token.endswith("insufficient"):
            return (
                ChargeOutcome.FAILED,
                "insufficient_funds",
                "The card has insufficient funds",
            )
        if token.endswith("action"):
            return ChargeOutcome.REQUIRES_ACTION, None, "Additional authentication required"
        return ChargeOutcome.SUCCEEDED, None, None

    @staticmethod
    def _card_display(token: str) -> tuple[str, str]:
        if token.endswith("decline"):
            return "visa", "0002"
        if token.endswith("insufficient"):
            return "visa", "9995"
        if token.endswith("action"):
            return "mastercard", "3184"
        return "visa", "4242"

    def _emit(self, event_type: str, data: dict) -> None:
        self._events.append(
            GatewayEvent(external_id=f"evt_{uuid.uuid4().hex[:16]}", type=event_type, payload=data)
        )
