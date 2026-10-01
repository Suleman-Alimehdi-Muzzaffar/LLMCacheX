"""API response schemas (Pydantic). Kept out of the core library."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """GET /api/health"""

    status: str
    service: str
    cache_backend: str = "sqlite"


class StatsResponse(BaseModel):
    """GET /api/stats — real counters from the LLMCacheX runtime state."""

    total_requests: int
    cache_hits: int
    cache_misses: int
    semantic_hits: int = 0
    hit_rate: float
    stored_entries: int
    expired_entries: int
    retry_count: int
    rate_limit_events: int
    successful_calls: int
    failed_calls: int
    avg_execution_latency_ms: float
    latest_execution_latency_ms: float
    total_execution_time_ms: float


class CacheEntryResponse(BaseModel):
    """Metadata for one cache row — never the response payload."""

    request_hash: str
    created_at: str
    expires_at: str | None
    expired: bool


class CacheListResponse(BaseModel):
    """GET /api/cache"""

    entries: list[CacheEntryResponse]
    count: int


class ClearCacheResponse(BaseModel):
    """DELETE /api/cache"""

    message: str
    cleared: bool


class ActivityEventResponse(BaseModel):
    """One bounded activity event (metadata only)."""

    type: str
    function: str
    timestamp: str
    latency_ms: float | None


class ActivityResponse(BaseModel):
    """GET /api/activity"""

    events: list[ActivityEventResponse]
    count: int


# ------------------------------------------------------------- analytics ----


class AnalyticsSummaryResponse(BaseModel):
    """GET /api/analytics/summary — aggregated real analytics history.

    ``None`` values mean "unknown / no data" (e.g. no token or pricing
    information exists yet) and must not be rendered as zero.
    """

    total_requests: int
    cache_hits: int
    cache_misses: int
    semantic_hits: int = 0
    hit_rate: float
    successful_requests: int
    failed_requests: int
    retry_count: int
    rate_limit_events: int
    total_execution_time_ms: float
    average_latency_ms: float | None
    total_tokens: int | None
    estimated_cost: float | None
    estimated_cost_saved: float | None


class TimeseriesPointResponse(BaseModel):
    """One time bucket of request activity."""

    timestamp: str
    requests: int
    cache_hits: int
    cache_misses: int
    average_latency_ms: float | None
    estimated_cost: float | None


class TimeseriesResponse(BaseModel):
    """GET /api/analytics/timeseries"""

    bucket: str
    hours: int
    points: list[TimeseriesPointResponse]
    count: int


class ProviderAnalyticsResponse(BaseModel):
    """Aggregated request data for one provider."""

    provider: str
    requests: int
    cache_hits: int
    cache_misses: int
    total_tokens: int | None
    estimated_cost: float | None


class UsageAnalyticsResponse(BaseModel):
    """GET /api/analytics/usage — usage/cost totals (None = unknown)."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    request_units: int | None
    estimated_cost: float | None
    estimated_cost_saved: float | None


class ClearAnalyticsResponse(BaseModel):
    """DELETE /api/analytics — clears analytics only, never the cache."""

    message: str
    cleared: bool
    events_cleared: int
