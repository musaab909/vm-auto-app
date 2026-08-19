"""Jinja2 environment and CSRF protection for the portal."""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.security import load_value, sign_value
from app.services.errors import PermissionDeniedError

TEMPLATE_DIR = "app/portal/templates"
CSRF_SALT = "csrf"
CSRF_MAX_AGE = 60 * 60 * 8
CSRF_FIELD = "csrf_token"

templates = Jinja2Templates(directory=TEMPLATE_DIR)


def money(cents: int | None, currency: str = "EUR") -> str:
    """Render integer minor units for display. Never used for arithmetic."""
    if cents is None:
        return "—"
    return f"{cents / 100:,.2f} {currency}"


def megabytes(mb: int | None) -> str:
    if mb is None:
        return "—"
    return f"{mb // 1024} GB" if mb >= 1024 else f"{mb} MB"


def status_tone(status: str) -> str:
    """Map a status onto a CSS tone class."""
    value = str(status)
    if value in {"running", "active", "paid", "succeeded", "available"}:
        return "ok"
    if value in {"error", "failed", "uncollectible", "terminated"}:
        return "bad"
    if value in {"suspended", "past_due", "open", "queued", "quarantined"}:
        return "warn"
    return "muted"


templates.env.filters["money"] = money
templates.env.filters["megabytes"] = megabytes
templates.env.filters["status_tone"] = status_tone


def issue_csrf_token(session_marker: str) -> str:
    """Mint a CSRF token bound to the caller's session cookie.

    Binding to the session value means a token minted for one user cannot be
    replayed in another user's browser.
    """
    return sign_value(session_marker, CSRF_SALT)


def verify_csrf(request: Request, token: str | None) -> None:
    from app.config import get_settings

    marker = request.cookies.get(get_settings().session_cookie_name, "")
    if not token:
        raise PermissionDeniedError("Missing CSRF token. Reload the page and try again.")

    value = load_value(token, CSRF_SALT, CSRF_MAX_AGE)
    if value is None or value != marker:
        raise PermissionDeniedError("Invalid CSRF token. Reload the page and try again.")
