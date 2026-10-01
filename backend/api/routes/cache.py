"""Cache metadata endpoints: GET /api/cache and DELETE /api/cache."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..schemas import CacheListResponse, ClearCacheResponse
from ..services import MetricsService
from .deps import get_service

router = APIRouter()


@router.get("/api/cache", response_model=CacheListResponse, tags=["cache"])
def list_cache(
    service: MetricsService = Depends(get_service),
) -> CacheListResponse:
    """Return safe metadata (hash/timestamps/status) for cache entries.

    Response payloads, prompts and any potentially sensitive content are
    never included.
    """
    return service.cache_entries()


@router.delete("/api/cache", response_model=ClearCacheResponse, tags=["cache"])
def clear_cache(
    service: MetricsService = Depends(get_service),
) -> ClearCacheResponse:
    """Remove all cache entries from the local SQLite cache.

    Runtime metrics/activity history are intentionally kept. This is a
    local developer tool: no authentication in Phase 6.
    """
    return service.clear_cache()
