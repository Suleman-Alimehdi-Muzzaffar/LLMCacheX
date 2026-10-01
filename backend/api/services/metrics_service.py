"""Read-only views over real LLMCacheX state for the dashboard API.

The service reads the same SQLite cache file that ``@cached_call`` writes
(counters, bounded activity events and cache entry metadata), so every
number the dashboard shows comes from actual runtime behavior.
"""

from __future__ import annotations

import os
from decimal import Decimal
from os import PathLike
from pathlib import Path

from llmcachex.analytics import AnalyticsStore
from llmcachex.metrics import MetricsRecorder
from llmcachex.semantic import SemanticStore
from llmcachex.storage import (
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    STRATEGIES,
    CacheStorage,
    create_storage,
)

from ..schemas import (
    ActivityEventResponse,
    ActivityResponse,
    AnalyticsSummaryResponse,
    CacheEntryResponse,
    CacheListResponse,
    ClearAnalyticsResponse,
    ClearCacheResponse,
    ProviderAnalyticsResponse,
    StatsResponse,
    TimeseriesPointResponse,
    TimeseriesResponse,
    UsageAnalyticsResponse,
)

DEFAULT_CACHE_DB = "llmcachex.db"
CACHE_DB_ENV_VAR = "LLMCACHEX_CACHE_DB"
CACHE_STRATEGY_ENV_VAR = "CACHE_STRATEGY"
REDIS_URL_ENV_VAR = "REDIS_URL"
REDIS_PREFIX_ENV_VAR = "REDIS_KEY_PREFIX"


def get_cache_db_path() -> str:
    """Return the cache database path from the environment.

    Defaults to ``llmcachex.db`` (relative to the working directory the
    API server was started from). Override with the
    ``LLMCACHEX_CACHE_DB`` environment variable.
    """
    return os.environ.get(CACHE_DB_ENV_VAR, DEFAULT_CACHE_DB)


def get_cache_strategy() -> str:
    """Return the cache backend strategy from the environment.

    Defaults to ``"sqlite"``. Override with the ``CACHE_STRATEGY``
    environment variable. Unknown values raise ``ValueError`` — the API
    never silently falls back to another backend.
    """
    strategy = os.environ.get(CACHE_STRATEGY_ENV_VAR, "sqlite").strip().lower()
    if strategy not in STRATEGIES:
        raise ValueError(
            f"Unsupported cache strategy {strategy!r} in "
            f"{CACHE_STRATEGY_ENV_VAR}. Supported strategies: "
            f"{', '.join(STRATEGIES)}."
        )
    return strategy


def get_redis_url() -> str:
    """Return the Redis URL from the environment (placeholder default)."""
    return os.environ.get(REDIS_URL_ENV_VAR, DEFAULT_REDIS_URL)


def get_redis_prefix() -> str:
    """Return the Redis key namespace from the environment."""
    return os.environ.get(REDIS_PREFIX_ENV_VAR, DEFAULT_KEY_PREFIX)


def _to_float(value: Decimal | None) -> float | None:
    """Serialize an exact Decimal cost as a JSON number (None stays None)."""
    return None if value is None else float(value)


class MetricsService:
    """Facade over a cache backend + :class:`MetricsRecorder`."""

    def __init__(
        self,
        cache_db: str | PathLike[str],
        *,
        strategy: str = "sqlite",
        redis_url: str | None = None,
        redis_prefix: str | None = None,
    ) -> None:
        """Open the cache backend for reading.

        Args:
            cache_db: Path of the SQLite file used by the library (the
                cache itself for ``"sqlite"``; metrics/analytics storage
                for ``"redis"``).
            strategy: ``"sqlite"`` or ``"redis"``.
            redis_url: Redis URL for ``"redis"`` (defaults to the local
                development URL); ignored otherwise.
            redis_prefix: Redis key namespace; ignored otherwise.
        """
        self._cache_db = Path(cache_db)
        self._strategy = strategy
        storage: CacheStorage = create_storage(
            strategy,
            cache_path=self._cache_db,
            redis_url=redis_url,
            redis_prefix=redis_prefix,
        )
        self._storage = storage
        self._metrics = MetricsRecorder(self._cache_db)
        self._analytics = AnalyticsStore(self._cache_db)
        self._semantic = SemanticStore(self._cache_db)

    @property
    def cache_db(self) -> Path:
        """Path of the backing SQLite file (metrics/analytics)."""
        return self._cache_db

    @property
    def cache_backend(self) -> str:
        """Selected cache backend (``"sqlite"`` or ``"redis"``)."""
        return self._strategy

    def stats(self) -> StatsResponse:
        """Aggregate real counters plus cache entry counts."""
        snapshot = self._metrics.snapshot()
        entries = self._storage.list_entries()
        return StatsResponse(
            total_requests=snapshot.total_requests,
            cache_hits=snapshot.cache_hits,
            cache_misses=snapshot.cache_misses,
            semantic_hits=snapshot.semantic_hits,
            hit_rate=round(snapshot.hit_rate, 4),
            stored_entries=len(entries),
            expired_entries=sum(1 for entry in entries if entry.expired),
            retry_count=snapshot.retry_count,
            rate_limit_events=snapshot.rate_limit_events,
            successful_calls=snapshot.successful_calls,
            failed_calls=snapshot.failed_calls,
            avg_execution_latency_ms=round(
                snapshot.avg_execution_latency_ms, 3
            ),
            latest_execution_latency_ms=round(
                snapshot.latest_execution_latency_ms, 3
            ),
            total_execution_time_ms=round(
                snapshot.total_execution_time_ms, 3
            ),
        )

    def cache_entries(self) -> CacheListResponse:
        """Return safe metadata for all cache rows (no response payloads)."""
        metas = self._storage.list_entries()
        return CacheListResponse(
            entries=[
                CacheEntryResponse(
                    request_hash=entry.request_hash,
                    created_at=entry.created_at,
                    expires_at=entry.expires_at,
                    expired=entry.expired,
                )
                for entry in metas
            ],
            count=len(metas),
        )

    def clear_cache(self) -> ClearCacheResponse:
        """Delete all cache entries (metrics history is kept).

        Associated semantic index rows are removed as well so no
        orphaned vectors outlive their exact entries.
        """
        self._storage.clear()
        self._semantic.clear()
        return ClearCacheResponse(message="Cache cleared", cleared=True)

    def activity(self, limit: int) -> ActivityResponse:
        """Return the most recent bounded activity events, newest first."""
        events = self._metrics.recent_events(limit=limit)
        return ActivityResponse(
            events=[
                ActivityEventResponse(
                    type=event.type,
                    function=event.function,
                    timestamp=event.timestamp,
                    latency_ms=event.latency_ms,
                )
                for event in events
            ],
            count=len(events),
        )

    def close(self) -> None:
        """Close underlying connections (used by tests/teardown)."""
        self._storage.close()
        self._metrics.close()
        self._analytics.close()
        self._semantic.close()

    # ------------------------------------------------------------ analytics ---

    def analytics_summary(self) -> AnalyticsSummaryResponse:
        """Aggregate the retained analytics history.

        Unknown usage/cost values are returned as ``None``, never as
        fabricated zeros.
        """
        summary = self._analytics.summary()
        return AnalyticsSummaryResponse(
            total_requests=summary.total_requests,
            cache_hits=summary.cache_hits,
            cache_misses=summary.cache_misses,
            semantic_hits=summary.semantic_hits,
            hit_rate=round(summary.hit_rate, 4),
            successful_requests=summary.successful_requests,
            failed_requests=summary.failed_requests,
            retry_count=summary.retry_count,
            rate_limit_events=summary.rate_limit_events,
            total_execution_time_ms=round(summary.total_execution_time_ms, 3),
            average_latency_ms=(
                None
                if summary.average_latency_ms is None
                else round(summary.average_latency_ms, 3)
            ),
            total_tokens=summary.total_tokens,
            estimated_cost=_to_float(summary.estimated_cost),
            estimated_cost_saved=_to_float(summary.estimated_cost_saved),
        )

    def analytics_timeseries(self, *, bucket: str, hours: int) -> TimeseriesResponse:
        """Request activity bucketed by hour or day (newest data within
        the requested window; empty buckets omitted)."""
        points = self._analytics.timeseries(bucket=bucket, hours=hours)
        return TimeseriesResponse(
            bucket=bucket,
            hours=hours,
            points=[
                TimeseriesPointResponse(
                    timestamp=point.timestamp,
                    requests=point.requests,
                    cache_hits=point.cache_hits,
                    cache_misses=point.cache_misses,
                    average_latency_ms=(
                        None
                        if point.average_latency_ms is None
                        else round(point.average_latency_ms, 3)
                    ),
                    estimated_cost=_to_float(point.estimated_cost),
                )
                for point in points
            ],
            count=len(points),
        )

    def analytics_providers(self) -> list[ProviderAnalyticsResponse]:
        """Request aggregates grouped by provider (``"unknown"`` when no
        provider metadata has been recorded — never a fabricated name)."""
        return [
            ProviderAnalyticsResponse(
                provider=entry.provider,
                requests=entry.requests,
                cache_hits=entry.cache_hits,
                cache_misses=entry.cache_misses,
                total_tokens=entry.total_tokens,
                estimated_cost=_to_float(entry.estimated_cost),
            )
            for entry in self._analytics.providers()
        ]

    def analytics_usage(self) -> UsageAnalyticsResponse:
        """Usage/cost totals; ``None`` means no such data was recorded."""
        totals = self._analytics.usage_totals()
        return UsageAnalyticsResponse(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            request_units=totals.request_units,
            estimated_cost=_to_float(totals.estimated_cost),
            estimated_cost_saved=_to_float(totals.estimated_cost_saved),
        )

    def clear_analytics(self) -> ClearAnalyticsResponse:
        """Delete analytics history only — cache entries are untouched."""
        removed = self._analytics.clear()
        return ClearAnalyticsResponse(
            message="Analytics cleared",
            cleared=True,
            events_cleared=removed,
        )
