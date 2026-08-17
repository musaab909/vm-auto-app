"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1 import api_router
from app.config import get_settings
from app.db import engine
from app.logging_config import configure_logging
from app.portal.admin_routes import router as admin_portal_router
from app.portal.routes import router as portal_router
from app.portal.templating import templates
from app.services.errors import AuthenticationError, DomainError

log = structlog.get_logger(__name__)

STATIC_DIR = "app/portal/static"


def create_app() -> FastAPI:
    # Fetched for its side effect: `get_settings()` runs the production safety
    # check, so a misconfigured deployment fails at import rather than at the
    # first request that happens to touch the unsafe setting.
    get_settings()
    configure_logging()

    app = FastAPI(
        title="HostPlane",
        description="VPS hosting control plane on VMware vSphere",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    app.include_router(api_router)
    # Registered before the customer portal so /admin/* is matched by the staff
    # router rather than by a customer-facing catch-all.
    app.include_router(admin_portal_router)
    app.include_router(portal_router)

    try:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    except RuntimeError:  # pragma: no cover - only when running from another cwd
        log.warning("static.mount_failed", directory=STATIC_DIR)

    _register_middleware(app)
    _register_error_handlers(app)
    _register_health(app)
    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(
            request_id=request_id, path=request.url.path, method=request.method
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "path", "method")

        response.headers["x-request-id"] = request_id
        # Baseline hardening. The portal renders user-supplied labels, so a strict
        # CSP and nosniff are what keep a hostile server label from becoming XSS.
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
        if get_settings().is_production:
            response.headers.setdefault(
                "strict-transport-security", "max-age=31536000; includeSubDomains"
            )
        return response


def _is_api_request(request: Request) -> bool:
    """True for JSON API paths.

    Keyed on the routing split rather than the `Accept` header: clients are
    inconsistent about `Accept` (curl and many HTTP libraries send `*/*`), and a
    browser navigation that fell through to a JSON body instead of the login page
    would be a confusing dead end.
    """
    return request.url.path.startswith("/api/")


def _wants_html(request: Request) -> bool:
    """True when the response should be a rendered page.

    HTMX requests are excluded: swapping a full error page into a fragment target
    would replace part of the UI with a nested document.
    """
    if _is_api_request(request):
        return False
    return request.headers.get("hx-request") != "true"


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> Response:
        # Unauthenticated portal navigations get a login redirect; API clients get
        # a 401 they can act on.
        if isinstance(exc, AuthenticationError) and not _is_api_request(request):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)

        if _wants_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": exc.message, "code": exc.code, "status": exc.status_code},
                status_code=exc.status_code,
            )

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "code": exc.code, "details": exc.details},
        )

    @app.exception_handler(500)
    async def internal_error_handler(request: Request, exc: Exception) -> Response:
        log.exception("request.unhandled_error", path=request.url.path)
        if _wants_html(request):
            return HTMLResponse("<h1>Something went wrong</h1>", status_code=500)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "code": "internal_error", "details": {}},
        )


def _register_health(app: FastAPI) -> None:
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        """Liveness: the process is up. Deliberately does not touch the database."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        """Readiness: dependencies this process cannot serve traffic without."""
        checks: dict[str, str] = {}
        healthy = True

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"
            healthy = False

        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "checks": checks},
        )


app = create_app()
