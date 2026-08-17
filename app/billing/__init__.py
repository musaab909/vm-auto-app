"""Billing engine and payment gateway factory."""

from __future__ import annotations

from functools import lru_cache

from app.billing.fake_gateway import FakeGateway
from app.billing.gateway import (
    ChargeOutcome,
    ChargeResult,
    GatewayError,
    GatewayEvent,
    PaymentGateway,
)

__all__ = [
    "ChargeOutcome",
    "ChargeResult",
    "FakeGateway",
    "GatewayError",
    "GatewayEvent",
    "PaymentGateway",
    "get_gateway",
    "reset_gateway_cache",
]


@lru_cache(maxsize=1)
def get_gateway() -> PaymentGateway:
    """Return the configured payment gateway.

    Only `FakeGateway` ships today. Wiring a real provider means adding a module
    that implements `PaymentGateway` and one branch here; nothing in invoicing or
    dunning changes.
    """
    return FakeGateway()


def reset_gateway_cache() -> None:
    get_gateway.cache_clear()
