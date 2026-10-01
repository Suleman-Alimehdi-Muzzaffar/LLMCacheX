"""Lightweight runtime metrics for LLMCacheX.

Counters and a bounded activity history are persisted in the same SQLite
file as the cache entries, so a separate local process (the Phase 6
dashboard API) can read the real runtime state of an application using
``@cached_call``.

Design constraints:
    - Standard library only (sqlite3, threading, dataclasses).
    - No Redis, no external monitoring service, no extra database file.
    - Activity history is bounded (``MAX_EVENTS``); old events are trimmed.
    - Counters distinguish CACHE HIT from CACHE MISS -> actual execution.
    - Thread-safe within one process; cross-process safety relies on
      SQLite's own file locking (write transactions are short).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path

MAX_EVENTS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_counters (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    function TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    latency_ms REAL
);
"""

@dataclass
class MetricsSnapshot:
    """Point-in-time view of all tracked counters."""

    total_requests: int
    cache_hits: int
    cache_misses: int
    successful_calls: int
    failed_calls: int
    retry_count: int
    rate_limit_events: int
    total_execution_time_ms: float
    execution_count: int
    latest_execution_latency_ms: float
    semantic_hits: int = 0

    @property
    def hit_rate(self) -> float:
        """Fraction of requests served from cache (0.0 when no requests)."""
        if self.total_requests == 0:
            return 0.0
        return self.cache_hits / self.total_requests

    @property
    def avg_execution_latency_ms(self) -> float:
        """Average latency of actual function executions (0.0 if none)."""
        if self.execution_count == 0:
            return 0.0
        return self.total_execution_time_ms / self.execution_count


@dataclass
class ActivityEvent:
    """A single bounded activity record (metadata only, no payloads)."""

    type: str
    function: str
    timestamp: str
    latency_ms: float | None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetricsRecorder:
    """Record cache/execution metrics into a SQLite cache database.

    Args:
        db_path: Path of the cache database file (same file used by
            :class:`~llmcachex.storage.SQLiteStorage`).
        max_events: Bounded activity history — only the newest
            ``max_events`` rows are kept (default :data:`MAX_EVENTS`).
    """

    def __init__(
        self,
        db_path: str | PathLike[str],
        *,
        max_events: int = MAX_EVENTS,
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
            self._connection.commit()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:  # pragma: no cover - defensive
            raise sqlite3.ProgrammingError("MetricsRecorder connection is closed.")
        return self._connection

    def _record(
        self,
        counter_updates: dict[str, float],
        event: tuple[str, str, float | None] | None = None,
        counter_sets: dict[str, float] | None = None,
    ) -> None:
        """Apply counter updates and, optionally, append one event.

        ``counter_updates`` are accumulated (``value += delta``);
        ``counter_sets`` replace the stored value (for gauges such as the
        latest latency). Everything happens in a single transaction so
        related counters stay consistent (e.g.
        ``total_requests == cache_hits + cache_misses``).
        """
        with self._lock:
            try:
                for name, delta in counter_updates.items():
                    self._conn().execute(
                        """
                        INSERT INTO metrics_counters (name, value) VALUES (?, ?)
                        ON CONFLICT(name) DO UPDATE
                        SET value = value + excluded.value
                        """,
                        (name, delta),
                    )
                for name, value in (counter_sets or {}).items():
                    self._conn().execute(
                        """
                        INSERT INTO metrics_counters (name, value) VALUES (?, ?)
                        ON CONFLICT(name) DO UPDATE
                        SET value = excluded.value
                        """,
                        (name, value),
                    )
                if event is not None:
                    event_type, function, latency_ms = event
                    self._conn().execute(
                        "INSERT INTO activity_events "
                        "(type, function, timestamp, latency_ms) "
                        "VALUES (?, ?, ?, ?)",
                        (event_type, function, _utcnow_iso(), latency_ms),
                    )
                    self._conn().execute(
                        "DELETE FROM activity_events WHERE id <= "
                        "(SELECT MAX(id) - ? FROM activity_events)",
                        (self._max_events,),
                    )
                self._conn().commit()
            except Exception:
                # Never leave a failed write transaction open: it can hold
                # the database write lock and block storage/analytics.
                self._conn().rollback()
                raise

    # ------------------------------------------------------------- records ---

    def record_cache_hit(self, function: str, latency_ms: float) -> None:
        """Record a cache HIT (request served without external execution)."""
        self._record(
            {"total_requests": 1, "cache_hits": 1},
            ("cache_hit", function, latency_ms),
        )

    def record_semantic_hit(self, function: str, latency_ms: float) -> None:
        """Record a SEMANTIC HIT (similar prior request reused).

        A semantic hit is also a cache hit: ``cache_hits`` (and
        ``total_requests``) increase as usual, while ``semantic_hits``
        tracks the semantic subset so ``exact = cache_hits -
        semantic_hits`` stays derivable and existing dashboards keep
        working.
        """
        self._record(
            {"total_requests": 1, "cache_hits": 1, "semantic_hits": 1},
            ("semantic_hit", function, latency_ms),
        )

    def record_cache_miss(self, function: str) -> None:
        """Record a cache MISS (this request requires actual execution)."""
        self._record(
            {"total_requests": 1, "cache_misses": 1},
            ("cache_miss", function, None),
        )

    def record_execution(self, function: str, latency_ms: float) -> None:
        """Record one successful actual function execution with its latency."""
        self._record(
            {
                "successful_calls": 1,
                "total_execution_time_ms": latency_ms,
                "execution_count": 1,
            },
            ("function_execution", function, latency_ms),
            # Gauge, not a delta: the latest latency replaces the old one.
            counter_sets={"latest_execution_latency_ms": latency_ms},
        )

    def record_failure(self, function: str) -> None:
        """Record a logical call that ultimately failed (never cached)."""
        self._record({"failed_calls": 1}, ("execution_failed", function, None))

    def record_retry(self, function: str) -> None:
        """Record one retry attempt."""
        self._record({"retry_count": 1}, ("retry", function, None))

    def record_rate_limit_wait(self, function: str) -> None:
        """Record that execution had to wait for rate-limit capacity."""
        self._record({"rate_limit_events": 1}, ("rate_limit", function, None))

    # -------------------------------------------------------------- queries ---

    def snapshot(self) -> MetricsSnapshot:
        """Return the current counter values (missing counters read as 0)."""
        with self._lock:
            rows = self._conn().execute(
                "SELECT name, value FROM metrics_counters"
            ).fetchall()
        values = {name: value for name, value in rows}
        return MetricsSnapshot(
            total_requests=int(values.get("total_requests", 0)),
            cache_hits=int(values.get("cache_hits", 0)),
            cache_misses=int(values.get("cache_misses", 0)),
            semantic_hits=int(values.get("semantic_hits", 0)),
            successful_calls=int(values.get("successful_calls", 0)),
            failed_calls=int(values.get("failed_calls", 0)),
            retry_count=int(values.get("retry_count", 0)),
            rate_limit_events=int(values.get("rate_limit_events", 0)),
            total_execution_time_ms=float(
                values.get("total_execution_time_ms", 0.0)
            ),
            execution_count=int(values.get("execution_count", 0)),
            latest_execution_latency_ms=float(
                values.get("latest_execution_latency_ms", 0.0)
            ),
        )

    def recent_events(self, limit: int = 50) -> list[ActivityEvent]:
        """Return up to ``limit`` most recent events, newest first."""
        bounded = max(0, min(limit, self._max_events))
        with self._lock:
            rows = self._conn().execute(
                "SELECT type, function, timestamp, latency_ms "
                "FROM activity_events ORDER BY id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [
            ActivityEvent(
                type=row[0], function=row[1], timestamp=row[2], latency_ms=row[3]
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the recorder connection; repeated calls are safe."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
