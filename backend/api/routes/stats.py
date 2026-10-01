"""GET /api/stats — real runtime statistics from the LLMCacheX state."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..schemas import StatsResponse
from ..services import MetricsService
from .deps import get_service

router = APIRouter()


@router.get("/api/stats", response_model=StatsResponse, tags=["stats"])
def stats(service: MetricsService = Depends(get_service)) -> StatsResponse:
    """Return counters measured from actual cache/execution behavior.

    ``total_requests == cache_hits + cache_misses``; only misses trigger
    actual function execution. Latency figures cover executions only,
    never cache hits.
    """
    return service.stats()
