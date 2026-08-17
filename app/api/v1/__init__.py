"""Version 1 of the JSON API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, auth, billing, catalog, instances, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(instances.router)
api_router.include_router(billing.router)
api_router.include_router(webhooks.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
