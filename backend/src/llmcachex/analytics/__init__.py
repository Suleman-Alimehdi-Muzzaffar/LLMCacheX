"""Runtime analytics: events, usage metadata, cost estimation, storage.

This package is provider-independent. It records structured request
events (cache hits/misses, retries, rate-limit waits), aggregates them
for the dashboard API, and represents provider-supplied usage and
configurable cost estimates — never fabricating tokens or prices.

Typical usage::

    from llmcachex.analytics import AnalyticsStore, RequestEvent, EventType

    store = AnalyticsStore("llmcachex.db")
    store.record(RequestEvent(
        event_type=EventType.CACHE_MISS,
        function_name="app.generate",
        request_hash="abc...",
        cache_status="miss",
        success=True,
    ))
"""

from .cost import PricingConfig, coerce_decimal, estimate_cost
from .events import (
    EVENT_TYPES,
    HIT_TYPES,
    REQUEST_EVENT_TYPES,
    EventType,
    HitType,
    RequestEvent,
)
from .metrics import (
    DEFAULT_MAX_EVENTS,
    AnalyticsStore,
    AnalyticsSummary,
    PriorExecution,
    ProviderAnalytics,
    TimeSeriesPoint,
    UsageTotals,
)
from .usage import Usage, coerce_usage

__all__ = [
    "DEFAULT_MAX_EVENTS",
    "EVENT_TYPES",
    "HIT_TYPES",
    "REQUEST_EVENT_TYPES",
    "AnalyticsStore",
    "AnalyticsSummary",
    "EventType",
    "HitType",
    "PriorExecution",
    "PricingConfig",
    "ProviderAnalytics",
    "RequestEvent",
    "TimeSeriesPoint",
    "Usage",
    "UsageTotals",
    "coerce_decimal",
    "coerce_usage",
    "estimate_cost",
]
