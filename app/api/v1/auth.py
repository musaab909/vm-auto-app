"""Authentication and API token routes."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentAccount, CurrentUser, DbSession, client_ip
from app.config import get_settings
from app.models.identity import ApiToken
from app.schemas import (
    AccountOut,
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenOut,
    LoginRequest,
    RegisterRequest,
    UserOut,
)
from app.security import sign_session
from app.services import auth as auth_service
from app.services.errors import NotFoundError

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, user_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        sign_session(user_id),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        # Lax rather than Strict so following a link from a billing email lands
        # the customer logged in, while still blocking cross-site form posts.
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, session: DbSession, response: Response):
    settings = get_settings()
    ip = client_ip(request)

    auth_service.check_rate_limit(
        session,
        key=f"register:{ip}",
        limit=settings.signup_rate_limit_per_hour,
        window_seconds=3600,
    )

    user, _account = auth_service.register_user(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        account_name=payload.account_name,
        source_ip=ip,
    )
    _set_session_cookie(response, user.id)
    return user


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, request: Request, session: DbSession, response: Response):
    settings = get_settings()
    ip = client_ip(request)

    # Keyed on the email as well as the IP so an attacker rotating source
    # addresses still cannot grind a single account.
    auth_service.check_rate_limit(
        session,
        key=f"login:{ip}:{payload.email.lower()}",
        limit=settings.login_rate_limit_per_minute,
        window_seconds=60,
    )

    user = auth_service.authenticate(
        session, email=payload.email, password=payload.password, source_ip=ip
    )
    _set_session_cookie(response, user.id)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response):
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.get("/account", response_model=AccountOut)
def current_account(account: CurrentAccount):
    return account


@router.post("/verify-email/{token}", response_model=UserOut)
def verify_email(token: str, session: DbSession):
    return auth_service.verify_email(session, token)


@router.get("/tokens", response_model=list[ApiTokenOut])
def list_tokens(session: DbSession, user: CurrentUser, account: CurrentAccount):
    return (
        session.execute(
            select(ApiToken)
            .where(ApiToken.account_id == account.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post("/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    payload: ApiTokenCreate, session: DbSession, user: CurrentUser, account: CurrentAccount
):
    token, plaintext = auth_service.create_api_token(
        session,
        user=user,
        account=account,
        name=payload.name,
        expires_in_days=payload.expires_in_days,
    )
    return ApiTokenCreated(token=ApiTokenOut.model_validate(token), plaintext=plaintext)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(token_id: str, session: DbSession, user: CurrentUser, account: CurrentAccount):
    token = session.get(ApiToken, token_id)
    # Scoped to the caller's account so one customer cannot revoke another's token.
    if token is None or token.account_id != account.id:
        raise NotFoundError("API token not found")
    auth_service.revoke_api_token(session, token, actor_user_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
