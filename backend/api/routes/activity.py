"""GET /api/activity — recent bounded runtime activity."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..schemas import ActivityResponse
from ..services import MetricsService
from .deps import get_service

router = APIRouter()


@router.get("/api/activity", response_model=ActivityResponse, tags=["activity"])
def activity(
    limit: int = Query(default=50, ge=1, le=200),
    service: MetricsService = Depends(get_service),
) -> ActivityResponse:
    """Return recent activity events (newest first, bounded to 200).

    Event types: ``cache_hit``, ``cache_miss``, ``function_execution``,
    ``execution_failed``, ``retry``, ``rate_limit``. Only metadata
    (function name, timestamp, latency) is exposed — never prompts or
    responses.
    """
    return service.activity(limit=limit)
