"""Shared FastAPI dependencies for the dashboard API."""

from __future__ import annotations

from fastapi import Request

from ..services import MetricsService


def get_service(request: Request) -> MetricsService:
    """Return the app-scoped :class:`MetricsService` (one per app)."""
    return request.app.state.service
