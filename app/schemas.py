"""Pydantic request and response models for the JSON API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import (
    InstanceStatus,
    InvoiceStatus,
    JobStatus,
    JobType,
    SubscriptionStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    full_name: str | None = Field(default=None, max_length=200)
    account_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(ORMModel):
    id: str
    email: str
    full_name: str | None
    role: str
    status: str
    email_verified_at: datetime | None
    created_at: datetime


class AccountOut(ORMModel):
    id: str
    name: str
    status: str
    currency: str
    balance_cents: int
    billing_email: str | None
    created_at: datetime


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiTokenOut(ORMModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiTokenCreated(BaseModel):
    token: ApiTokenOut
    # Returned exactly once; only a hash is persisted.
    plaintext: str


# --- catalog ----------------------------------------------------------------


class PlanOut(ORMModel):
    id: str
    slug: str
    name: str
    description: str | None
    vcpu: int
    ram_mb: int
    disk_gb: int
    bandwidth_tb: int
    price_cents: int
    setup_fee_cents: int
    currency: str
    billing_period: str


class RegionOut(ORMModel):
    id: str
    slug: str
    name: str
    country: str | None


class ImageOut(ORMModel):
    id: str
    slug: str
    name: str
    family: str
    version: str
    min_disk_gb: int
    default_username: str


# --- SSH keys ---------------------------------------------------------------


class SshKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=20)


class SshKeyOut(ORMModel):
    id: str
    name: str
    fingerprint: str
    created_at: datetime


# --- instances --------------------------------------------------------------


class InstanceCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    plan: str
    image: str
    region: str
    hostname: str | None = Field(default=None, max_length=63)
    ssh_key_ids: list[str] = Field(default_factory=list)

    @field_validator("hostname")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.lower() if v else v


class InstanceOut(ORMModel):
    id: str
    label: str
    hostname: str
    status: InstanceStatus
    vcpu: int
    ram_mb: int
    disk_gb: int
    primary_ip: str | None
    created_at: datetime
    provisioned_at: datetime | None
    error_message: str | None


class InstanceCreated(BaseModel):
    instance: InstanceOut
    job_id: str
    # Shown once at creation; only a hash is stored.
    root_password: str


class InstanceActionRequest(BaseModel):
    hard: bool = False


class ResizeRequest(BaseModel):
    plan: str


class RebuildRequest(BaseModel):
    image: str | None = None
    ssh_key_ids: list[str] = Field(default_factory=list)
    # Reinstalling destroys the existing disk, so the caller must say so explicitly.
    confirm: bool = False


class JobOut(ORMModel):
    id: str
    type: JobType
    status: JobStatus
    attempts: int
    progress: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ConsoleOut(BaseModel):
    """A minted console handle.

    Carries no credential: the WebMKS ticket is only released by redeeming the
    handle, which works exactly once.
    """

    handle: str
    redeem_url: str
    expires_at: datetime


class ConsoleSessionOut(BaseModel):
    """Connection details, released once per handle."""

    instance_id: str
    host: str | None
    port: int | None
    url: str | None
    mode: str
    viewer_url: str | None


# --- billing ----------------------------------------------------------------


class InvoiceLineOut(ORMModel):
    description: str
    quantity: int
    unit_price_cents: int
    amount_cents: int
    period_start: datetime | None
    period_end: datetime | None


class InvoiceOut(ORMModel):
    id: str
    number: str
    status: InvoiceStatus
    currency: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    amount_paid_cents: int
    credit_applied_cents: int
    amount_due_cents: int
    issued_at: datetime | None
    due_at: datetime | None
    paid_at: datetime | None
    lines: list[InvoiceLineOut] = Field(default_factory=list)


class SubscriptionOut(ORMModel):
    id: str
    status: SubscriptionStatus
    unit_price_cents: int
    currency: str
    billing_period: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    instance_id: str | None


class PaymentMethodCreate(BaseModel):
    # A gateway-issued token, never raw card data — no PAN ever reaches this API.
    token: str = Field(min_length=3, max_length=200)
    set_default: bool = True


class PaymentMethodOut(ORMModel):
    id: str
    gateway: str
    kind: str
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    is_default: bool


class LedgerEntryOut(ORMModel):
    created_at: datetime
    type: str
    amount_cents: int
    balance_after_cents: int
    currency: str
    description: str


# --- errors -----------------------------------------------------------------


class ErrorOut(BaseModel):
    error: str
    code: str
    details: dict = Field(default_factory=dict)
