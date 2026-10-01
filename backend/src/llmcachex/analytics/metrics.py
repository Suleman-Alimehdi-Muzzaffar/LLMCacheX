"""Persistent, bounded analytics store + aggregations.

Events are persisted in a dedicated ``analytics_events`` table inside the
*same* SQLite file as the cache — conceptually separate from
``cache_entries`` (which never gains analytics columns). Standard library
only: sqlite3, threading, dataclasses, decimal.

Design notes:
    - Retention: the table is trimmed to the last ``max_events`` rows
      (default :data:`DEFAULT_MAX_EVENTS`) after every insert, so history
      cannot grow without bound.
    - Aggregations load the bounded row set and fold it in Python so
      ``Decimal`` costs stay exact (no float accumulation). With the
      default retention this is a single query over <= 1000 rows.
    - Costs are stored as decimal strings (exact round trip).
    - No prompts, responses, API keys or environment values are stored —
      metadata only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from os import PathLike
from pathlib import Path

from .events import REQUEST_EVENT_TYPES, EventType, RequestEvent

__all__ = [
    "DEFAULT_MAX_EVENTS",
    "AnalyticsStore",
    "AnalyticsSummary",
    "TimeSeriesPoint",
    "ProviderAnalytics",
    "UsageTotals",
    "PriorExecution",
]

#: Default retention: how many analytics events are kept.
DEFAULT_MAX_EVENTS = 1000

_TIME_BUCKETS = ("hour", "day")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    function_name TEXT NOT NULL,
    cache_status TEXT,
    latency_ms REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    success INTEGER,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    request_units INTEGER,
    estimated_cost TEXT,
    estimated_cost_saved TEXT,
    hit_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_analytics_events_hash
    ON analytics_events(request_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_events_timestamp
    ON analytics_events(timestamp);
"""

_COLUMNS = (
    "timestamp, event_type, request_hash, function_name, cache_status, "
    "latency_ms, retry_count, success, provider, model, input_tokens, "
    "output_tokens, total_tokens, request_units, estimated_cost, "
    "estimated_cost_saved, hit_type"
)


def _decimal_to_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_to_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@dataclass(frozen=True)
class AnalyticsSummary:
    """Aggregated view over the recorded analytics history."""

    total_requests: int
    cache_hits: int
    cache_misses: int
    hit_rate: float
    successful_requests: int
    failed_requests: int
    retry_count: int
    rate_limit_events: int
    total_execution_time_ms: float
    average_latency_ms: float | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    estimated_cost_saved: Decimal | None
    semantic_hits: int = 0


@dataclass(frozen=True)
class TimeSeriesPoint:
    """One time bucket of request activity."""

    timestamp: str
    requests: int
    cache_hits: int
    cache_misses: int
    average_latency_ms: float | None
    estimated_cost: Decimal | None


@dataclass(frozen=True)
class ProviderAnalytics:
    """Request aggregates grouped by provider (``"unknown"`` when unset)."""

    provider: str
    requests: int
    cache_hits: int
    cache_misses: int
    total_tokens: int | None
    estimated_cost: Decimal | None


@dataclass(frozen=True)
class UsageTotals:
    """Usage/cost totals; ``None`` fields mean "no data", not zero."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    request_units: int | None
    estimated_cost: Decimal | None
    estimated_cost_saved: Decimal | None


@dataclass(frozen=True)
class PriorExecution:
    """Metadata of the most recent known execution for a request hash."""

    provider: str | None
    model: str | None
    estimated_cost: Decimal | None


class AnalyticsStore:
    """Persist and aggregate analytics events in a SQLite cache file.

    Args:
        db_path: Path of the SQLite database (same file the cache uses).
        max_events: Retention limit — only the newest ``max_events``
            events are kept (default :data:`DEFAULT_MAX_EVENTS`).
    """

    def __init__(
        self,
        db_path: str | PathLike[str],
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int):
            raise ValueError(f"max_events must be an int, got {max_events!r}.")
        if max_events < 1:
            raise ValueError(f"max_events must be >= 1, got {max_events}.")
        self._db_path = Path(db_path)
        self._max_events = max_events
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=5.0
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._migrate()
            self._connection.commit()

    def _migrate(self) -> None:
        """Add ``hit_type`` to databases created before semantic hits."""
        columns = {
            row[1]
            for row in self._conn().execute("PRAGMA table_info(analytics_events)")
        }
        if "hit_type" not in columns:
            self._conn().execute(
                "ALTER TABLE analytics_events ADD COLUMN hit_type TEXT"
            )

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:  # pragma: no cover - defensive
            raise sqlite3.ProgrammingError("AnalyticsStore connection is closed.")
        return self._connection

    # ------------------------------------------------------------- writes ---

    def record(self, event: RequestEvent) -> int:
        """Persist one event (assigns ``event.event_id``) and trim.

        Returns:
            The new row id.
        """
        with self._lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    f"INSERT INTO analytics_events ({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.timestamp,
                        event.event_type,
                        event.request_hash,
                        event.function_name,
                        event.cache_status,
                        event.latency_ms,
                        int(event.retry_count),
                        (
                            None
                            if event.success is None
                            else (1 if event.success else 0)
                        ),
                        event.provider,
                        event.model,
                        event.input_tokens,
                        event.output_tokens,
                        event.total_tokens,
                        event.request_units,
                        _decimal_to_text(event.estimated_cost),
                        _decimal_to_text(event.estimated_cost_saved),
                        event.hit_type,
                    ),
                )
                event.event_id = cursor.lastrowid
                # Retention: keep only the newest ``max_events`` records.
                conn.execute(
                    "DELETE FROM analytics_events WHERE id <= "
                    "(SELECT MAX(id) - ? FROM analytics_events)",
                    (self._max_events,),
                )
                conn.commit()
            except Exception:
                # A failed write may sit in an open transaction holding the
                # database write lock; roll back so other writers (metrics,
                # storage) are not blocked until the connection closes.
                conn.rollback()
                raise
            return event.event_id

    def clear(self) -> int:
        """Delete all analytics history (cache entries are untouched).

        Returns:
            Number of events removed.
        """
        with self._lock:
            try:
                count = self._conn().execute(
                    "SELECT COUNT(*) FROM analytics_events"
                ).fetchone()[0]
                self._conn().execute("DELETE FROM analytics_events")
                self._conn().commit()
            except Exception:
                # Never leave a failed write transaction holding the lock.
                self._conn().rollback()
                raise
            return int(count)

    def close(self) -> None:
        """Close the store connection; repeated calls are safe."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # ------------------------------------------------------------ helpers ---

    def count(self) -> int:
        """Number of retained analytics events."""
        with self._lock:
            row = self._conn().execute(
                "SELECT COUNT(*) FROM analytics_events"
            ).fetchone()
        return int(row[0])

    def resolve_prior_context(self, request_hash: str) -> PriorExecution | None:
        """Most recent miss event for this hash regardless of known cost.

        Lets an inherited ``estimated_cost_saved`` stay ``None`` while the
        provider/model labels of previous executions are still reused.
        """
        with self._lock:
            row = self._conn().execute(
                "SELECT provider, model, estimated_cost "
                "FROM analytics_events "
                "WHERE request_hash = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT 1",
                (request_hash, EventType.CACHE_MISS),
            ).fetchone()
        if row is None:
            return None
        return PriorExecution(
            provider=row[0], model=row[1], estimated_cost=_text_to_decimal(row[2])
        )

    def _load(
        self, *, since: str | None = None, request_only: bool = False
    ) -> list[sqlite3.Row] | list[tuple]:
        with self._lock:
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            try:
                if since is not None:
                    rows = conn.execute(
                        f"SELECT {_COLUMNS} FROM analytics_events "
                        "WHERE timestamp >= ? ORDER BY timestamp ASC",
                        (since,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT {_COLUMNS} FROM analytics_events ORDER BY id ASC"
                    ).fetchall()
            finally:
                conn.row_factory = None
        if request_only:
            rows = [row for row in rows if row["event_type"] in REQUEST_EVENT_TYPES]
        return rows

    # -------------------------------------------------------- aggregations ---

    def summary(self) -> AnalyticsSummary:
        """Aggregate the whole retained history into one summary."""
        rows = self._load()
        total_requests = hits = misses = 0
        successful = failed = retries = rate_limits = 0
        semantic_hits = 0
        execution_ms = 0.0
        execution_count = 0
        total_tokens: int | None = None
        estimated_cost: Decimal | None = None
        estimated_cost_saved: Decimal | None = None
        has_cost = has_saved = False

        for row in rows:
            etype = row["event_type"]
            if etype in REQUEST_EVENT_TYPES:
                total_requests += 1
                if etype == EventType.CACHE_HIT:
                    hits += 1
                    successful += 1  # a hit is always a successful request
                    if row["hit_type"] == "semantic":
                        semantic_hits += 1
                else:
                    misses += 1
                    success = row["success"]
                    if success == 1:
                        successful += 1
                    elif success == 0:
                        failed += 1
                    latency = row["latency_ms"]
                    if latency is not None:
                        execution_ms += float(latency)
                        execution_count += 1
            elif etype == EventType.RETRY:
                retries += 1
            elif etype == EventType.RATE_LIMIT_WAIT:
                rate_limits += 1

            if row["total_tokens"] is not None:
                total_tokens = (total_tokens or 0) + int(row["total_tokens"])
            if row["estimated_cost"] is not None:
                estimated_cost = (estimated_cost or Decimal(0)) + _text_to_decimal(
                    row["estimated_cost"]
                )
                has_cost = True
            if row["estimated_cost_saved"] is not None:
                estimated_cost_saved = (
                    estimated_cost_saved or Decimal(0)
                ) + _text_to_decimal(row["estimated_cost_saved"])
                has_saved = True

        return AnalyticsSummary(
            total_requests=total_requests,
            cache_hits=hits,
            cache_misses=misses,
            hit_rate=(hits / total_requests) if total_requests else 0.0,
            successful_requests=successful,
            failed_requests=failed,
            retry_count=retries,
            rate_limit_events=rate_limits,
            total_execution_time_ms=execution_ms,
            average_latency_ms=(
                (execution_ms / execution_count) if execution_count else None
            ),
            total_tokens=total_tokens,
            estimated_cost=estimated_cost if has_cost else None,
            estimated_cost_saved=estimated_cost_saved if has_saved else None,
            semantic_hits=semantic_hits,
        )

    def timeseries(
        self, *, bucket: str = "hour", hours: int = 24
    ) -> list[TimeSeriesPoint]:
        """Request activity grouped into ``hour`` or ``day`` buckets.

        Only the last ``hours`` are considered. Buckets with no request
        events are omitted (frontend charts can bridge gaps). Latency is
        execution latency (cache-miss events); ``None`` when a bucket had
        no executions. ``estimated_cost`` is ``None`` when no event in the
        bucket had a known cost.
        """
        if bucket not in _TIME_BUCKETS:
            raise ValueError(
                f"bucket must be one of {_TIME_BUCKETS}, got {bucket!r}."
            )
        if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
            raise ValueError(f"hours must be a positive int, got {hours!r}.")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        rows = self._load(since=cutoff, request_only=True)

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            prefix = row["timestamp"][:13] if bucket == "hour" else row["timestamp"][:10]
            grouped[prefix].append(row)

        points: list[TimeSeriesPoint] = []
        for prefix in sorted(grouped):
            bucket_rows = grouped[prefix]
            requests = len(bucket_rows)
            hits = sum(1 for r in bucket_rows if r["event_type"] == EventType.CACHE_HIT)
            misses = requests - hits
            latencies = [
                float(r["latency_ms"])
                for r in bucket_rows
                if r["event_type"] == EventType.CACHE_MISS
                and r["latency_ms"] is not None
            ]
            costs = [
                _text_to_decimal(r["estimated_cost"])
                for r in bucket_rows
                if r["estimated_cost"] is not None
            ]
            stamp = (
                f"{prefix}:00:00+00:00"
                if bucket == "hour"
                else f"{prefix}T00:00:00+00:00"
            )
            points.append(
                TimeSeriesPoint(
                    timestamp=stamp,
                    requests=requests,
                    cache_hits=hits,
                    cache_misses=misses,
                    average_latency_ms=(
                        sum(latencies) / len(latencies) if latencies else None
                    ),
                    estimated_cost=(
                        sum(costs, Decimal(0)) if costs else None
                    ),
                )
            )
        return points

    def providers(self) -> list[ProviderAnalytics]:
        """Request aggregates grouped by provider name.

        Events without a provider are grouped under ``"unknown"`` (real
        value — never a fabricated provider name).
        """
        rows = self._load(request_only=True)
        buckets: dict[str, dict] = {}
        for row in rows:
            name = row["provider"] or "unknown"
            entry = buckets.setdefault(
                name,
                {
                    "requests": 0,
                    "hits": 0,
                    "misses": 0,
                    "tokens": 0,
                    "has_tokens": False,
                    "cost": Decimal(0),
                    "has_cost": False,
                },
            )
            entry["requests"] += 1
            if row["event_type"] == EventType.CACHE_HIT:
                entry["hits"] += 1
            else:
                entry["misses"] += 1
            if row["total_tokens"] is not None:
                entry["tokens"] += int(row["total_tokens"])
                entry["has_tokens"] = True
            if row["estimated_cost"] is not None:
                entry["cost"] += _text_to_decimal(row["estimated_cost"])
                entry["has_cost"] = True

        result = [
            ProviderAnalytics(
                provider=name,
                requests=entry["requests"],
                cache_hits=entry["hits"],
                cache_misses=entry["misses"],
                total_tokens=entry["tokens"] if entry["has_tokens"] else None,
                estimated_cost=entry["cost"] if entry["has_cost"] else None,
            )
            for name, entry in buckets.items()
        ]
        result.sort(key=lambda item: (-item.requests, item.provider))
        return result

    def usage_totals(self) -> UsageTotals:
        """Sum of usage/cost fields across retained events.

        ``None`` means no event carried that data (unknown — not zero).
        """
        rows = self._load()
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None
        request_units: int | None = None
        cost: Decimal | None = None
        saved: Decimal | None = None
        for row in rows:
            if row["input_tokens"] is not None:
                input_tokens = (input_tokens or 0) + int(row["input_tokens"])
            if row["output_tokens"] is not None:
                output_tokens = (output_tokens or 0) + int(row["output_tokens"])
            if row["total_tokens"] is not None:
                total_tokens = (total_tokens or 0) + int(row["total_tokens"])
            if row["request_units"] is not None:
                request_units = (request_units or 0) + int(row["request_units"])
            if row["estimated_cost"] is not None:
                cost = (cost or Decimal(0)) + _text_to_decimal(row["estimated_cost"])
            if row["estimated_cost_saved"] is not None:
                saved = (saved or Decimal(0)) + _text_to_decimal(
                    row["estimated_cost_saved"]
                )
        return UsageTotals(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_units=request_units,
            estimated_cost=cost,
            estimated_cost_saved=saved,
        )
