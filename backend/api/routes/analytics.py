"""Analytics endpoints — summary, timeseries, providers, usage, clear.

All values are computed from the retained ``analytics_events`` history
(metadata only: no prompts, responses or secrets). Unknown usage/cost
data is returned as ``null``, never as an invented number.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..schemas import (
    AnalyticsSummaryResponse,
    ClearAnalyticsResponse,
    ProviderAnalyticsResponse,
    TimeseriesResponse,
    UsageAnalyticsResponse,
)
from ..services import MetricsService
from .deps import get_service

router = APIRouter()


@router.get(
    "/api/analytics/summary",
    response_model=AnalyticsSummaryResponse,
    tags=["analytics"],
)
def analytics_summary(
    service: MetricsService = Depends(get_service),
) -> AnalyticsSummaryResponse:
    """Aggregated real analytics: requests, hits/misses, retries,
    rate-limit waits, latency, tokens and estimated cost/savings.

    ``null`` means the value is unknown (no usage/pricing data yet).
    """
    return service.analytics_summary()


@router.get(
    "/api/analytics/timeseries",
    response_model=TimeseriesResponse,
    tags=["analytics"],
)
def analytics_timeseries(
    bucket: Literal["hour", "day"] = Query(default="hour"),
    hours: int = Query(default=24, ge=1, le=8760),
    service: MetricsService = Depends(get_service),
) -> TimeseriesResponse:
    """Request activity over time, bucketed hourly (default) or daily.

    Only non-empty buckets are returned; latency is execution latency
    (``null`` for buckets without executions) and ``estimated_cost`` is
    ``null`` when no event in the bucket had a known cost.
    """
    return service.analytics_timeseries(bucket=bucket, hours=hours)


@router.get(
    "/api/analytics/providers",
    response_model=list[ProviderAnalyticsResponse],
    tags=["analytics"],
)
def analytics_providers(
    service: MetricsService = Depends(get_service),
) -> list[ProviderAnalyticsResponse]:
    """Aggregated request data grouped by provider.

    Events without provider metadata are grouped under ``"unknown"`` —
    provider names are never invented.
    """
    return service.analytics_providers()


@router.get(
    "/api/analytics/usage",
    response_model=UsageAnalyticsResponse,
    tags=["analytics"],
)
def analytics_usage(
    service: MetricsService = Depends(get_service),
) -> UsageAnalyticsResponse:
    """Usage totals (tokens, request units) and estimated cost/savings.

    ``null`` fields mean no provider usage/pricing information exists.
    """
    return service.analytics_usage()


@router.delete(
    "/api/analytics",
    response_model=ClearAnalyticsResponse,
    tags=["analytics"],
)
def clear_analytics(
    service: MetricsService = Depends(get_service),
) -> ClearAnalyticsResponse:
    """Clear the analytics event history.

    Cache entries and Phase 6 metrics counters are NOT touched — cache
    clearing is the separate ``DELETE /api/cache`` operation.
    """
    return service.clear_analytics()
