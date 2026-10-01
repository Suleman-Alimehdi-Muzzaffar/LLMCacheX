"""Structured request-activity events (metadata only).

One logical call produces at most one *request-level* event
(``cache_hit`` or ``cache_miss`` — the miss also carries the execution
outcome), plus one event per discrete ``retry`` attempt and per
``rate_limit_wait``. The vocabulary is a small extensible set; unknown
event types are rejected when recording.

Provider/usage/cost fields are ``None`` until real provider metadata
exists. Nothing here ever stores prompts, responses or API keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

__all__ = [
    "EventType",
    "EVENT_TYPES",
    "REQUEST_EVENT_TYPES",
    "HitType",
    "HIT_TYPES",
    "RequestEvent",
    "utc_now_iso",
]


class EventType:
    """Event vocabulary (plain string constants for easy storage)."""

    CACHE_HIT: Final = "cache_hit"
    CACHE_MISS: Final = "cache_miss"
    RETRY: Final = "retry"
    RATE_LIMIT_WAIT: Final = "rate_limit_wait"


class HitType:
    """How a cache hit was produced (exact hash vs semantic similarity)."""

    EXACT: Final = "exact"
    SEMANTIC: Final = "semantic"


#: Valid ``hit_type`` values for hit events (``None`` elsewhere).
HIT_TYPES: Final[frozenset[str]] = frozenset(
    {
        HitType.EXACT,
        HitType.SEMANTIC,
    }
)


#: All currently valid event types. Extensible by adding a constant here.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        EventType.CACHE_HIT,
        EventType.CACHE_MISS,
        EventType.RETRY,
        EventType.RATE_LIMIT_WAIT,
    }
)

#: Event types that count as one logical request.
REQUEST_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {EventType.CACHE_HIT, EventType.CACHE_MISS}
)


def utc_now_iso() -> str:
    """Current UTC time as a sortable ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RequestEvent:
    """One analytics record.

    Args:
        event_type: One of :data:`EVENT_TYPES`.
        function_name: Dotted function label (``module.qualname``).
        request_hash: Deterministic request hash (no prompt content).
        timestamp: ISO-8601 UTC timestamp.
        cache_status: ``"hit"``, ``"miss"``, or ``None`` for events that
            are not request-level (retry, rate-limit wait).
        latency_ms: Measured latency (hit: lookup, miss: execution).
        retry_count: Retries observed during this request.
        success: For a miss: did the execution succeed. ``True`` for
            hits, ``None`` where not applicable.
        provider: Provider name if known, else ``None``.
        model: Model name if known, else ``None``.
        input_tokens / output_tokens / total_tokens: Provider-reported
            usage, or ``None`` when unknown.
        request_units: Optional provider-specific billing units.
        estimated_cost: Cost of executing this request (miss only), or
            ``None`` when usage/pricing are unavailable.
        estimated_cost_saved: For a hit: the previously known execution
            cost that was avoided, or ``None`` when unknown.
        hit_type: For hit events: ``"exact"`` or ``"semantic"``;
            ``None`` for misses and non-request events.
        event_id: Assigned by the store after persistence.
    """

    event_type: str
    function_name: str
    request_hash: str
    timestamp: str = field(default_factory=utc_now_iso)
    cache_status: str | None = None
    latency_ms: float | None = None
    retry_count: int = 0
    success: bool | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    request_units: int | None = None
    estimated_cost: Decimal | None = None
    estimated_cost_saved: Decimal | None = None
    hit_type: str | None = None
    event_id: int | None = None

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(
                f"Unknown event type {self.event_type!r}; "
                f"expected one of {sorted(EVENT_TYPES)}."
            )
        if self.cache_status not in (None, "hit", "miss"):
            raise ValueError(
                f"cache_status must be 'hit', 'miss' or None, "
                f"got {self.cache_status!r}."
            )
        if self.hit_type is not None and self.hit_type not in HIT_TYPES:
            raise ValueError(
                f"hit_type must be 'exact', 'semantic' or None, "
                f"got {self.hit_type!r}."
            )
