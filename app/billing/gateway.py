"""Payment gateway contract.

The billing engine talks only to this interface, so adding Stripe, Adyen or
PayPal later means writing one new module implementing `PaymentGateway` and
changing a single factory line — no invoicing, dunning or webhook code moves.

Amounts crossing this boundary are always integer minor units plus an explicit
ISO-4217 currency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ChargeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUIRES_ACTION = "requires_action"


@dataclass(slots=True)
class GatewayCustomer:
    ref: str
    email: str | None = None


@dataclass(slots=True)
class GatewayMethod:
    ref: str
    kind: str = "card"
    brand: str | None = None
    last4: str | None = None
    exp_month: int | None = None
    exp_year: int | None = None


@dataclass(slots=True)
class ChargeResult:
    outcome: ChargeOutcome
    ref: str | None = None
    amount_cents: int = 0
    currency: str = "EUR"
    failure_code: str | None = None
    failure_message: str | None = None
    # Set when the gateway needs the customer to complete 3-D Secure or similar.
    action_url: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ChargeOutcome.SUCCEEDED


@dataclass(slots=True)
class RefundResult:
    ref: str | None
    amount_cents: int
    succeeded: bool
    failure_message: str | None = None


@dataclass(slots=True)
class GatewayEvent:
    """A normalised webhook event.

    `external_id` must be stable per event so redelivery is detectable — that
    uniqueness is what stops a replayed webhook from crediting an invoice twice.
    """

    external_id: str
    type: str
    payload: dict = field(default_factory=dict)


class GatewayError(Exception):
    """Transport or configuration failure — distinct from a declined charge.

    A decline is a `ChargeResult`, not an exception: it is an expected business
    outcome that drives dunning. This exception means the gateway could not be
    reached or answered nonsensically, and is retryable.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@runtime_checkable
class PaymentGateway(Protocol):
    name: str

    def create_customer(self, *, account_id: str, email: str | None) -> GatewayCustomer: ...

    def attach_payment_method(self, *, customer_ref: str, method_token: str) -> GatewayMethod: ...

    def detach_payment_method(self, *, method_ref: str) -> None: ...

    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        method_ref: str,
        customer_ref: str | None,
        idempotency_key: str,
        description: str | None = None,
    ) -> ChargeResult: ...

    def refund(
        self, *, charge_ref: str, amount_cents: int, idempotency_key: str
    ) -> RefundResult: ...

    def verify_webhook(self, *, payload: bytes, signature: str) -> bool: ...

    def parse_webhook(self, *, payload: bytes) -> GatewayEvent: ...
