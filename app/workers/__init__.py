"""Celery worker package."""

from __future__ import annotations

from app.workers.celery_app import celery_app

__all__ = ["celery_app"]
