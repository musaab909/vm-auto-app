"""Users, accounts, memberships, API tokens, quotas and audit logs."""

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
from app.models.enums import AccountStatus, UserRole, UserStatus

if TYPE_CHECKING:
    from app.models.compute import Instance


class User(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        EnumString(UserRole, 20), default=UserRole.CUSTOMER, nullable=False
    )
    status: Mapped[UserStatus] = mapped_column(
        EnumString(UserStatus, 20), default=UserStatus.ACTIVE, nullable=False
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verification_token: Mapped[str | None] = mapped_column(String(64), index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[AccountMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_staff(self) -> bool:
        return self.role in {UserRole.STAFF, UserRole.ADMIN}


class Account(UUIDPrimaryKey, Timestamped, Base):
    """The billing entity. Distinct from the login identity so a company can have
    several logins against one set of invoices."""

    __tablename__ = "accounts"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[AccountStatus] = mapped_column(
        EnumString(AccountStatus, 20), default=AccountStatus.ACTIVE, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)

    # Denormalised running balance in minor units. Positive means the customer holds
    # credit; negative means they owe money. Always moved via the ledger, never set
    # directly, so `ledger_entries` stays the auditable source of truth.
    balance_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    billing_email: Mapped[str | None] = mapped_column(String(320))
    company_name: Mapped[str | None] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(64))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(2))

    # Set when dunning suspends the account; cleared when the balance is settled.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(foreign_keys=[owner_user_id])
    members: Mapped[list[AccountMember]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    quota: Mapped[Quota | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    instances: Mapped[list[Instance]] = relationship(back_populates="account")


class AccountMember(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "account_members"
    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_account_members_pair"),)

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=False)

    account: Mapped[Account] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class ApiToken(UUIDPrimaryKey, Timestamped, Base):
    """Bearer tokens for the JSON API.

    Only a SHA-256 hash of the token is stored; the plaintext is shown once at
    creation. `prefix` is the non-secret leading segment, kept so the UI can
    identify a token without holding the secret.
    """

    __tablename__ = "api_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_tokens")


class Quota(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "quotas"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    max_instances: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_ips: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_vcpu: Mapped[int] = mapped_column(Integer, default=32, nullable=False)
    max_ram_mb: Mapped[int] = mapped_column(Integer, default=131072, nullable=False)
    max_disk_gb: Mapped[int] = mapped_column(Integer, default=4096, nullable=False)

    account: Mapped[Account] = relationship(back_populates="quota")


class SshKey(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "ssh_keys"
    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="uq_ssh_keys_account_fingerprint"),
    )

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)


class AuditLog(UUIDPrimaryKey, Base):
    """Append-only record of every state-changing action."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_target", "target_type", "target_id"),)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(64))
    source_ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class EmailOutbox(UUIDPrimaryKey, Timestamped, Base):
    """Rendered outbound mail.

    No SMTP provider was in scope, so mail is persisted here instead of being
    silently dropped: verification links and dunning notices stay inspectable in
    development and testing, and a real transport can drain this table later.
    """

    __tablename__ = "email_outbox"

    to_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class RateLimitBucket(UUIDPrimaryKey, Base):
    """Fixed-window rate limit counters.

    Kept in Postgres rather than Redis so the limiter still holds when the broker
    is unavailable — auth endpoints are exactly where you least want to fail open.
    """

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (UniqueConstraint("key", "window_start", name="uq_rate_limit_key_window"),)

    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PasswordResetToken(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    single_use: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
