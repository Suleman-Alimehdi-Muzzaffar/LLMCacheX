"""SQLite-backed response storage for LLMCacheX.

This module handles persistence only: serialized JSON responses keyed by
request hash, with optional UTC expiration timestamps. Hashing, decorator,
retry and rate-limit logic live elsewhere.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_hash TEXT UNIQUE NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
)
"""

_MIGRATION_ADD_EXPIRES_AT = (
    "ALTER TABLE cache_entries ADD COLUMN expires_at TEXT"
)


def _utcnow() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _parse_expires_at(value: str | None) -> datetime | None:
    """Parse a stored ``expires_at`` value, assuming UTC when naive."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class CacheEntry:
    """A cached response together with its optional expiration time."""

    response: object
    expires_at: datetime | None


@dataclass
class CacheEntryMeta:
    """Safe metadata about one cache row (never includes the response)."""

    request_hash: str
    created_at: str
    expires_at: str | None
    expired: bool


class SQLiteStorage:
    """Persist JSON-serializable responses in SQLite, keyed by request hash.

    The database file (and the ``cache_entries`` table) is created
    automatically on initialization, as are any missing parent
    directories of the database path.

    Thread model: all operations on one instance are serialized with an
    internal re-entrant lock, so a single instance may be shared safely
    by threads within one process. The storage is NOT multi-process safe:
    separate processes (or separate instances) open independent
    connections with no cross-connection coordination beyond SQLite's
    own locking.
    """

    def __init__(self, db_path: str | PathLike[str]) -> None:
        """Open (or create) the SQLite database and ensure the schema exists.

        Args:
            db_path: Filesystem path of the SQLite database file. Missing
                parent directories are created automatically.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            str(self._db_path), check_same_thread=False
        )
        assert self._connection is not None
        with self._lock:
            self._connection.execute(_SCHEMA)
            self._migrate()
            self._connection.commit()

    def _migrate(self) -> None:
        """Add ``expires_at`` to databases created before Phase 4."""
        columns = {
            row[1]
            for row in self._conn().execute("PRAGMA table_info(cache_entries)")
        }
        if "expires_at" not in columns:
            self._conn().execute(_MIGRATION_ADD_EXPIRES_AT)

    def _conn(self) -> sqlite3.Connection:
        """Return the live connection or raise if the storage is closed."""
        if self._connection is None:
            raise sqlite3.ProgrammingError(
                "SQLiteStorage connection is closed."
            )
        return self._connection

    def get_entry(self, request_hash: str) -> CacheEntry | None:
        """Return the cache entry for ``request_hash``, or None on miss.

        Expired entries are treated as cache misses: they are not returned
        (though they are left in the database rather than deleted).

        Args:
            request_hash: Deterministic hash identifying the request.

        Returns:
            The :class:`CacheEntry`, or ``None`` if missing or expired.
        """
        with self._lock:
            cursor = self._conn().execute(
                "SELECT response_json, expires_at FROM cache_entries "
                "WHERE request_hash = ?",
                (request_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            expires_at = _parse_expires_at(row[1])
            if expires_at is not None and expires_at <= _utcnow():
                return None
            return CacheEntry(
                response=json.loads(row[0]), expires_at=expires_at
            )

    def get(self, request_hash: str) -> object | None:
        """Return the cached Python object for ``request_hash``, or None on miss.

        Expired entries behave as cache misses and are not returned.

        Args:
            request_hash: Deterministic hash identifying the request.

        Returns:
            The deserialized response object, or ``None`` if not cached
            or expired.
        """
        entry = self.get_entry(request_hash)
        if entry is None:
            return None
        return entry.response

    def set(
        self,
        request_hash: str,
        response: object,
        expires_at: datetime | None = None,
    ) -> None:
        """Store ``response`` under ``request_hash``, updating on conflict.

        Args:
            request_hash: Deterministic hash identifying the request.
            response: JSON-serializable Python object to cache.
            expires_at: Optional timezone-aware UTC expiration time.
                ``None`` (the default) preserves the pre-Phase-4 behavior:
                the entry never expires. Naive datetimes are assumed to be
                UTC.

        Raises:
            TypeError: If ``expires_at`` is neither ``None`` nor a datetime.
        """
        if expires_at is not None and not isinstance(expires_at, datetime):
            raise TypeError(
                "expires_at must be a datetime or None, got "
                f"{type(expires_at).__name__!r}."
            )
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        response_json = json.dumps(response, ensure_ascii=False)
        created_at = _utcnow().isoformat()
        expires_value = expires_at.isoformat() if expires_at else None
        with self._lock:
            try:
                self._conn().execute(
                    """
                    INSERT INTO cache_entries
                        (request_hash, response_json, created_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(request_hash) DO UPDATE SET
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (request_hash, response_json, created_at, expires_value),
                )
                self._conn().commit()
            except Exception:
                # Release any lock taken by the failed write so other
                # connections (metrics, analytics) are not blocked.
                self._conn().rollback()
                raise

    def exists(self, request_hash: str) -> bool:
        """Return True if a valid (unexpired) entry for ``request_hash`` exists.

        Args:
            request_hash: Deterministic hash identifying the request.
        """
        return self.get_entry(request_hash) is not None

    def list_entries(self) -> list[CacheEntryMeta]:
        """Return metadata for every stored row, newest first.

        Only safe metadata is returned: request hash, timestamps and the
        computed expired flag. Response payloads are intentionally excluded.

        Returns:
            Row metadata ordered by descending ``id`` (most recent first).
        """
        now = _utcnow()
        with self._lock:
            rows = self._conn().execute(
                "SELECT request_hash, created_at, expires_at "
                "FROM cache_entries ORDER BY id DESC"
            ).fetchall()
        entries: list[CacheEntryMeta] = []
        for request_hash, created_at, expires_at in rows:
            parsed = _parse_expires_at(expires_at)
            entries.append(
                CacheEntryMeta(
                    request_hash=request_hash,
                    created_at=created_at,
                    expires_at=expires_at,
                    expired=parsed is not None and parsed <= now,
                )
            )
        return entries

    def clear(self) -> None:
        """Remove all cache entries without deleting the database file."""
        with self._lock:
            try:
                self._conn().execute("DELETE FROM cache_entries")
                self._conn().commit()
            except Exception:
                self._conn().rollback()
                raise

    def close(self) -> None:
        """Safely close the SQLite connection; repeated calls are safe."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
