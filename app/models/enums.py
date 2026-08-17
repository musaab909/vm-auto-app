"""Enumerations used across the schema.

Every enum inherits from `str` so values serialize as plain strings in JSON and
compare cleanly against database text.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DELINQUENT = "delinquent"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class InstanceStatus(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STARTING = "starting"
    REBOOTING = "rebooting"
    REBUILDING = "rebuilding"
    RESIZING = "resizing"
    SUSPENDED = "suspended"
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        return self in {InstanceStatus.DELETED}

    @property
    def is_transitional(self) -> bool:
        return self in {
            InstanceStatus.PENDING,
            InstanceStatus.PROVISIONING,
            InstanceStatus.STOPPING,
            InstanceStatus.STARTING,
            InstanceStatus.REBOOTING,
            InstanceStatus.REBUILDING,
            InstanceStatus.RESIZING,
            InstanceStatus.DELETING,
        }


class IPStatus(StrEnum):
    AVAILABLE = "available"
    ASSIGNED = "assigned"
    RESERVED = "reserved"
    QUARANTINED = "quarantined"


class IPVersion(StrEnum):
    V4 = "ipv4"
    V6 = "ipv6"


class JobType(StrEnum):
    PROVISION = "provision"
    START = "start"
    STOP = "stop"
    REBOOT = "reboot"
    REBUILD = "rebuild"
    RESIZE = "resize"
    DELETE = "delete"
    SUSPEND = "suspend"
    RESUME = "resume"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELED = "canceled"
    TERMINATED = "terminated"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    REQUIRES_ACTION = "requires_action"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class LedgerEntryType(StrEnum):
    CHARGE = "charge"
    PAYMENT = "payment"
    REFUND = "refund"
    CREDIT = "credit"
    ADJUSTMENT = "adjustment"


class BillingPeriod(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
