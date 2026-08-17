"""FastAPI dependencies: authentication, account resolution and authorization.

Ownership is enforced here rather than in each route. `get_owned_instance` is the
only supported way to load an instance from a path parameter, so no route can
accidentally authenticate a user and then act on somebody else's server.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.compute import Instance
from app.models.enums import AccountStatus, UserRole, UserStatus
from app.models.identity import Account, AccountMember, User
from app.security import load_session
from app.services import auth as auth_service
from app.services.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
)

DbSession = Annotated[Session, Depends(get_db)]


def client_ip(request: Request) -> str | None:
    """Best-effort client address.

    `X-Forwarded-For` is honoured because the app is expected to sit behind a
    reverse proxy. It is spoofable by a direct client, so it is used only for rate
    limiting and audit context, never for authorization.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_from_bearer(session: Session, header: str) -> tuple[User, Account] | None:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return auth_service.resolve_api_token(session, token.strip())


def _user_from_cookie(session: Session, request: Request) -> User | None:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    user_id = load_session(raw)
    if user_id is None:
        return None
    return session.get(User, user_id)


def get_current_user_optional(request: Request, session: DbSession) -> User | None:
    """Resolve the caller, if any. Used by pages that render for guests too."""
    header = request.headers.get("authorization")
    if header:
        try:
            resolved = _user_from_bearer(session, header)
        except AuthenticationError:
            return None
        if resolved is not None:
            user, account = resolved
            # Cached so `get_current_account` honours the token's account rather
            # than falling back to the user's first one.
            request.state.token_account_id = account.id
            return user

    return _user_from_cookie(session, request)


def get_current_user(request: Request, session: DbSession) -> User:
    header = request.headers.get("authorization")
    if header:
        resolved = _user_from_bearer(session, header)
        if resolved is None:
            raise AuthenticationError("Authorization header must be 'Bearer <token>'")
        user, account = resolved
        request.state.token_account_id = account.id
        return user

    cookie_user = _user_from_cookie(session, request)
    if cookie_user is None:
        raise AuthenticationError("Sign in to continue")
    if cookie_user.status is not UserStatus.ACTIVE:
        raise AuthenticationError("This account is not active")
    return cookie_user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]


def get_current_account(request: Request, session: DbSession, user: CurrentUser) -> Account:
    """Resolve the account the caller is acting on."""
    token_account_id = getattr(request.state, "token_account_id", None)
    if token_account_id:
        account = session.get(Account, token_account_id)
        if account is None:
            raise AuthenticationError("The account for this token no longer exists")
        return account

    return auth_service.primary_account(session, user)


CurrentAccount = Annotated[Account, Depends(get_current_account)]


def require_staff(user: CurrentUser) -> User:
    if user.role not in {UserRole.STAFF, UserRole.ADMIN}:
        raise PermissionDeniedError("Staff access is required")
    return user


def require_admin(user: CurrentUser) -> User:
    if user.role is not UserRole.ADMIN:
        raise PermissionDeniedError("Administrator access is required")
    return user


StaffUser = Annotated[User, Depends(require_staff)]
AdminUser = Annotated[User, Depends(require_admin)]


def require_active_account(account: CurrentAccount) -> Account:
    """Block ordering on accounts that are suspended or closed for non-payment."""
    if account.status in {AccountStatus.SUSPENDED, AccountStatus.CLOSED}:
        raise PermissionDeniedError(
            "This account is suspended. Settle any outstanding invoices to continue."
        )
    return account


ActiveAccount = Annotated[Account, Depends(require_active_account)]


def user_can_access_account(session: Session, user: User, account_id: str) -> bool:
    if user.role is UserRole.ADMIN:
        return True
    membership = session.execute(
        select(AccountMember).where(
            AccountMember.account_id == account_id, AccountMember.user_id == user.id
        )
    ).scalar_one_or_none()
    return membership is not None


def get_owned_instance(instance_id: str, session: DbSession, user: CurrentUser) -> Instance:
    """Load an instance the caller is allowed to act on.

    A missing instance and someone else's instance both return 404 on purpose:
    a 403 would confirm that the id exists and belongs to another customer.
    """
    instance = session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError("Instance not found")
    if not user_can_access_account(session, user, instance.account_id):
        raise NotFoundError("Instance not found")
    return instance


OwnedInstance = Annotated[Instance, Depends(get_owned_instance)]


def db_session() -> Iterator[Session]:
    yield from get_db()
