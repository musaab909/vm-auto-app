"""Domain exceptions.

These carry an HTTP status and a stable machine-readable code so the API layer
can translate them without every route re-deriving the mapping.
"""

from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(DomainError):
    status_code = 403
    code = "permission_denied"


class AuthenticationError(DomainError):
    status_code = 401
    code = "unauthenticated"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class QuotaExceededError(DomainError):
    status_code = 409
    code = "quota_exceeded"


class NoCapacityError(DomainError):
    """Raised when the platform physically cannot satisfy an order."""

    status_code = 503
    code = "no_capacity"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"


class InvalidStateError(DomainError):
    """The requested action is not legal from the resource's current state."""

    status_code = 409
    code = "invalid_state"


class PaymentError(DomainError):
    status_code = 402
    code = "payment_failed"


class DriverError(Exception):
    """Raised by hypervisor drivers. Retryable unless `permanent` is set."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.message = message
        self.permanent = permanent
