"""SQLAlchemy models.

Every model is imported here so `Base.metadata` is complete for Alembic
autogenerate and for `create_all()` in tests.
"""

from __future__ import annotations

from app.models.base import Base
from app.models.billing import (
    Invoice,
    InvoiceLine,
    LedgerEntry,
    Payment,
    PaymentMethod,
    Subscription,
    WebhookEvent,
)
from app.models.catalog import OsImage, Plan, PlanRegion, Region
from app.models.compute import ConsoleTicket, Instance, Job
from app.models.enums import (
    AccountStatus,
    BillingPeriod,
    InstanceStatus,
    InvoiceStatus,
    IPStatus,
    IPVersion,
    JobStatus,
    JobType,
    LedgerEntryType,
    PaymentStatus,
    SubscriptionStatus,
    UserRole,
    UserStatus,
)
from app.models.identity import (
    Account,
    AccountMember,
    ApiToken,
    AuditLog,
    EmailOutbox,
    PasswordResetToken,
    Quota,
    RateLimitBucket,
    SshKey,
    User,
)
from app.models.network import IpAddress, IpPool

# Grouped by domain rather than sorted alphabetically: the grouping mirrors the
# module layout and is what makes this list readable at a glance.
__all__ = [  # noqa: RUF022
    "Base",
    # identity
    "User",
    "Account",
    "AccountMember",
    "ApiToken",
    "Quota",
    "SshKey",
    "AuditLog",
    "EmailOutbox",
    "RateLimitBucket",
    "PasswordResetToken",
    # catalog
    "Region",
    "Plan",
    "PlanRegion",
    "OsImage",
    # network
    "IpPool",
    "IpAddress",
    # compute
    "Instance",
    "Job",
    "ConsoleTicket",
    # billing
    "Subscription",
    "Invoice",
    "InvoiceLine",
    "PaymentMethod",
    "Payment",
    "LedgerEntry",
    "WebhookEvent",
    # enums
    "UserRole",
    "UserStatus",
    "AccountStatus",
    "InstanceStatus",
    "IPStatus",
    "IPVersion",
    "JobType",
    "JobStatus",
    "SubscriptionStatus",
    "InvoiceStatus",
    "PaymentStatus",
    "LedgerEntryType",
    "BillingPeriod",
]
