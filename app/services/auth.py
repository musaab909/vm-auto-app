"""Registration, login, token and rate-limiting logic."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import UserRole, UserStatus
from app.models.identity import (
    Account,
    AccountMember,
    ApiToken,
    EmailOutbox,
    PasswordResetToken,
    RateLimitBucket,
    User,
)
from app.security import (
    generate_token,
    hash_password,
    hash_token,
    make_api_token,
    needs_rehash,
    utcnow,
    verify_password,
)
from app.services import audit, quota
from app.services.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)

MIN_PASSWORD_LENGTH = 10


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if password.isdigit() or password.isalpha():
        raise ValidationError("Password must mix letters with digits or symbols")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register_user(
    session: Session,
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    account_name: str | None = None,
    source_ip: str | None = None,
) -> tuple[User, Account]:
    """Create a user, their billing account and a verification token."""
    email = normalize_email(email)
    validate_password_strength(password)

    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("An account with that email address already exists")

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=(full_name or "").strip() or None,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        email_verification_token=generate_token(24),
    )
    session.add(user)

    try:
        session.flush()
    except IntegrityError as exc:
        # Two simultaneous signups for the same address: the unique index is the
        # real guard, the SELECT above is only a friendlier fast path.
        session.rollback()
        raise ConflictError("An account with that email address already exists") from exc

    settings = get_settings()
    account = Account(
        name=(account_name or full_name or email.split("@")[0])[:200],
        owner_user_id=user.id,
        billing_email=email,
        currency=settings.currency,
    )
    session.add(account)
    session.flush()

    session.add(AccountMember(account_id=account.id, user_id=user.id, role="owner"))
    quota.get_or_create_quota(session, account)

    queue_email(
        session,
        to_email=email,
        subject="Confirm your HostPlane account",
        template="verify_email",
        body=(
            f"Welcome to HostPlane.\n\n"
            f"Confirm your email address:\n"
            f"{settings.base_url}/verify?token={user.email_verification_token}\n"
        ),
    )

    audit.record(
        session,
        action="user.registered",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="user",
        target_id=user.id,
        source_ip=source_ip,
    )
    return user, account


def authenticate(
    session: Session, *, email: str, password: str, source_ip: str | None = None
) -> User:
    email = normalize_email(email)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        # Identical message and cost for both cases so the response cannot be used
        # to enumerate which addresses are registered.
        raise AuthenticationError("Email address or password is incorrect")

    if user.status is UserStatus.SUSPENDED:
        raise AuthenticationError("This account is suspended. Contact support.")
    if user.status is UserStatus.CLOSED:
        raise AuthenticationError("This account has been closed.")

    # Transparently upgrade hashes when Argon2 parameters change.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = utcnow()
    audit.record(
        session,
        action="user.login",
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        source_ip=source_ip,
    )
    return user


def verify_email(session: Session, token: str) -> User:
    user = session.execute(
        select(User).where(User.email_verification_token == token)
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("That verification link is not valid")
    user.email_verified_at = utcnow()
    user.email_verification_token = None
    audit.record(session, action="user.email_verified", actor_user_id=user.id, target_id=user.id)
    return user


def primary_account(session: Session, user: User) -> Account:
    account = (
        session.execute(
            select(Account)
            .join(AccountMember, AccountMember.account_id == Account.id)
            .where(AccountMember.user_id == user.id)
            .order_by(Account.created_at)
        )
        .scalars()
        .first()
    )
    if account is None:
        raise NotFoundError("This user has no billing account")
    return account


# --- API tokens -------------------------------------------------------------


def create_api_token(
    session: Session, *, user: User, account: Account, name: str, expires_in_days: int | None = None
) -> tuple[ApiToken, str]:
    """Create an API token. The plaintext is returned once and never stored."""
    plaintext, token_hash, prefix = make_api_token()
    token = ApiToken(
        user_id=user.id,
        account_id=account.id,
        name=name.strip()[:100] or "API token",
        token_hash=token_hash,
        prefix=prefix,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    session.add(token)
    session.flush()
    audit.record(
        session,
        action="api_token.created",
        actor_user_id=user.id,
        account_id=account.id,
        target_type="api_token",
        target_id=token.id,
    )
    return token, plaintext


def resolve_api_token(session: Session, plaintext: str) -> tuple[User, Account]:
    token = session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(plaintext))
    ).scalar_one_or_none()

    if token is None or token.revoked_at is not None:
        raise AuthenticationError("Invalid API token")
    if token.expires_at is not None and token.expires_at <= utcnow():
        raise AuthenticationError("This API token has expired")

    user = session.get(User, token.user_id)
    account = session.get(Account, token.account_id)
    if user is None or account is None:
        raise AuthenticationError("Invalid API token")
    if user.status is not UserStatus.ACTIVE:
        raise AuthenticationError("This account is not active")

    token.last_used_at = utcnow()
    return user, account


def revoke_api_token(session: Session, token: ApiToken, *, actor_user_id: str) -> None:
    token.revoked_at = utcnow()
    audit.record(
        session,
        action="api_token.revoked",
        actor_user_id=actor_user_id,
        account_id=token.account_id,
        target_type="api_token",
        target_id=token.id,
    )


# --- password reset ---------------------------------------------------------


def request_password_reset(session: Session, email: str) -> PasswordResetToken | None:
    """Issue a reset token. Returns None for unknown addresses.

    The caller must show the same confirmation either way — a different response
    for unknown addresses would leak which emails are registered.
    """
    user = session.execute(
        select(User).where(User.email == normalize_email(email))
    ).scalar_one_or_none()
    if user is None:
        return None

    plaintext = generate_token(32)
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        expires_at=utcnow() + timedelta(hours=2),
    )
    session.add(reset)

    settings = get_settings()
    queue_email(
        session,
        to_email=user.email,
        subject="Reset your HostPlane password",
        template="password_reset",
        body=(
            "A password reset was requested for your account.\n\n"
            f"{settings.base_url}/reset-password?token={plaintext}\n\n"
            "This link expires in 2 hours. If you did not request it, ignore this email.\n"
        ),
    )
    return reset


def complete_password_reset(session: Session, *, token: str, new_password: str) -> User:
    validate_password_strength(new_password)
    record = session.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
    ).scalar_one_or_none()

    if record is None or record.used_at is not None or record.expires_at <= utcnow():
        raise ValidationError("That reset link is invalid or has expired")

    user = session.get(User, record.user_id)
    if user is None:
        raise NotFoundError("User not found")

    user.password_hash = hash_password(new_password)
    record.used_at = utcnow()
    audit.record(session, action="user.password_reset", actor_user_id=user.id, target_id=user.id)
    return user


# --- rate limiting ----------------------------------------------------------


def check_rate_limit(session: Session, *, key: str, limit: int, window_seconds: int) -> None:
    """Fixed-window rate limit backed by Postgres.

    Deliberately not Redis-backed: auth endpoints must not fail open when the
    broker is unavailable.
    """
    now = utcnow()
    window_start = now - timedelta(seconds=now.timestamp() % window_seconds)
    window_start = window_start.replace(microsecond=0)

    bucket = session.execute(
        select(RateLimitBucket)
        .where(RateLimitBucket.key == key, RateLimitBucket.window_start == window_start)
        .with_for_update()
    ).scalar_one_or_none()

    if bucket is None:
        bucket = RateLimitBucket(key=key, window_start=window_start, count=1)
        session.add(bucket)
        try:
            session.flush()
        except IntegrityError:
            # Another request created the same bucket first; re-read and count on it.
            session.rollback()
            bucket = session.execute(
                select(RateLimitBucket)
                .where(RateLimitBucket.key == key, RateLimitBucket.window_start == window_start)
                .with_for_update()
            ).scalar_one()
            bucket.count += 1
            session.flush()
        return

    bucket.count += 1
    session.flush()

    if bucket.count > limit:
        raise RateLimitedError(
            "Too many attempts. Wait a minute and try again.",
            details={"limit": limit, "window_seconds": window_seconds},
        )


def queue_email(
    session: Session, *, to_email: str, subject: str, body: str, template: str | None = None
) -> EmailOutbox:
    """Persist an outbound email.

    No SMTP transport is configured, so mail is stored rather than dropped:
    verification and dunning notices stay inspectable, and a real sender can drain
    this table later.
    """
    message = EmailOutbox(to_email=to_email, subject=subject, body=body, template=template)
    session.add(message)
    return message
