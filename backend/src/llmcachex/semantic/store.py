"""Persistent semantic index (SQLite, sidecar to metrics/analytics).

Each row links one exact cache entry (by its deterministic request hash)
to the embedding of its semantic text plus the compatibility context
that entry was recorded with. Search filters rows to the compatible
context first (namespace, function, provider, model, embedding model and
dimension, semantic fields) and ranks the survivors by cosine similarity
— a linear scan, documented as suitable for small-to-medium caches, not
a vector database.

Eligibility is always re-validated against the configured cache backend
(``storage.get_entry``): expired or deleted exact entries make their
semantic rows ineligible, and stale rows are removed lazily on match
attempts. Embeddings here may indirectly represent user input; rows
carry no API keys, no credentials and no response payloads (only the
request hash points at the cached response).
"""

from __future__ import annotations

import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path

from .similarity import cosine_similarity

__all__ = [
    "DEFAULT_MAX_SEMANTIC_ENTRIES",
    "SemanticCandidate",
    "SemanticRecord",
    "SemanticStore",
    "pack_embedding",
    "unpack_embedding",
]

#: Default retention: how many semantic rows are kept (oldest trimmed).
DEFAULT_MAX_SEMANTIC_ENTRIES = 1000

#: How many top candidates the decorator validates against the backend.
VALIDATION_LIMIT = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT UNIQUE NOT NULL,
    embedding BLOB NOT NULL,
    embedding_model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    function_name TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    fields_signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_semantic_entries_context
    ON semantic_entries(namespace, function_name);
"""


def pack_embedding(vector: list[float] | tuple[float, ...]) -> bytes:
    """Serialize a float vector as little-endian float32 bytes."""
    return struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])


def unpack_embedding(blob: bytes, dimension: int) -> list[float] | None:
    """Deserialize float32 bytes; ``None`` when malformed (never raises)."""
    if dimension < 1 or len(blob) != dimension * 4:
        return None
    try:
        return list(struct.unpack(f"<{dimension}f", blob))
    except struct.error:
        return None


@dataclass(frozen=True)
class SemanticRecord:
    """One indexed semantic representation of an exact cache entry."""

    cache_key: str
    embedding: tuple[float, ...]
    embedding_model: str
    dimension: int
    namespace: str
    function_name: str
    provider: str | None
    model: str | None
    fields_signature: str
    created_at: str
    expires_at: str | None


@dataclass(frozen=True)
class SemanticCandidate:
    """A compatible index row ranked by similarity to the query."""

    cache_key: str
    similarity: float
    embedding_model: str
    function_name: str
    provider: str | None
    model: str | None


class SemanticStore:
    """SQLite-backed semantic index living beside metrics/analytics.

    Args:
        db_path: SQLite file (the same file the decorator uses for
            metrics/analytics, so the index persists across restarts).
        max_entries: Retention limit — only the newest ``max_entries``
            rows are kept (default
            :data:`DEFAULT_MAX_SEMANTIC_ENTRIES`).
    """

    def __init__(
        self,
        db_path: str | PathLike[str],
        *,
        max_entries: int = DEFAULT_MAX_SEMANTIC_ENTRIES,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError(
                f"max_entries must be an int, got {max_entries!r}."
            )
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}.")
        self._db_path = Path(db_path)
        self._max_entries = max_entries
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
            raise sqlite3.ProgrammingError("SemanticStore connection is closed.")
        return self._connection

    # ------------------------------------------------------------- writes ---

    def add(self, record: SemanticRecord) -> None:
        """Insert or replace the semantic row for an exact cache entry."""
        if not record.embedding:
            raise ValueError("Cannot index an empty embedding.")
        if record.dimension != len(record.embedding):
            raise ValueError(
                f"Embedding dimension {len(record.embedding)} does not match "
                f"declared dimension {record.dimension}."
            )
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO semantic_entries
                        (cache_key, embedding, embedding_model, dimension,
                         namespace, function_name, provider, model,
                         fields_signature, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        embedding = excluded.embedding,
                        embedding_model = excluded.embedding_model,
                        dimension = excluded.dimension,
                        namespace = excluded.namespace,
                        function_name = excluded.function_name,
                        provider = excluded.provider,
                        model = excluded.model,
                        fields_signature = excluded.fields_signature,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        record.cache_key,
                        pack_embedding(list(record.embedding)),
                        record.embedding_model,
                        record.dimension,
                        record.namespace,
                        record.function_name,
                        record.provider,
                        record.model,
                        record.fields_signature,
                        record.created_at,
                        record.expires_at,
                    ),
                )
                conn.execute(
                    "DELETE FROM semantic_entries WHERE id <= "
                    "(SELECT MAX(id) - ? FROM semantic_entries)",
                    (self._max_entries,),
                )
                conn.commit()
            except Exception:
                # Never leave a failed write holding the database lock.
                conn.rollback()
                raise

    def remove(self, cache_key: str) -> None:
        """Delete the semantic row for ``cache_key`` (lazy invalidation)."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM semantic_entries WHERE cache_key = ?",
                    (cache_key,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def clear(self) -> int:
        """Delete all semantic rows in this file; return the removal count."""
        with self._lock:
            conn = self._conn()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM semantic_entries"
                ).fetchone()[0]
                conn.execute("DELETE FROM semantic_entries")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return int(count)

    # -------------------------------------------------------------- search ---

    def find(
        self,
        query: list[float] | tuple[float, ...],
        *,
        namespace: str,
        function_name: str,
        provider: str | None,
        model: str | None,
        embedding_model: str,
        dimension: int,
        fields_signature: str,
        threshold: float,
        limit: int = VALIDATION_LIMIT,
    ) -> list[SemanticCandidate]:
        """Return compatible candidates at or above ``threshold``.

        Rows are filtered to the exact compatibility context first
        (``IS`` comparisons are NULL-safe), then ranked by cosine
        similarity, best first. Malformed rows are skipped. Callers must
        still validate each candidate against the cache backend (TTL /
        deletion) before use.
        """
        if not query:
            return []
        candidates: list[SemanticCandidate] = []
        with self._lock:
            rows = self._conn().execute(
                "SELECT cache_key, embedding, embedding_model, dimension, "
                "function_name, provider, model "
                "FROM semantic_entries "
                "WHERE namespace IS ? AND function_name IS ? "
                "AND provider IS ? AND model IS ? "
                "AND embedding_model IS ? AND dimension IS ? "
                "AND fields_signature IS ?",
                (
                    namespace,
                    function_name,
                    provider,
                    model,
                    embedding_model,
                    dimension,
                    fields_signature,
                ),
            ).fetchall()
        for row in rows:
            vector = unpack_embedding(row[1], int(row[3]))
            if vector is None:
                continue
            try:
                similarity = cosine_similarity(list(query), vector)
            except ValueError:
                continue
            if similarity >= threshold:
                candidates.append(
                    SemanticCandidate(
                        cache_key=row[0],
                        similarity=similarity,
                        embedding_model=row[2],
                        function_name=row[4],
                        provider=row[5],
                        model=row[6],
                    )
                )
        candidates.sort(key=lambda item: item.similarity, reverse=True)
        return candidates[: max(1, limit)]

    def count(self) -> int:
        """Number of retained semantic rows."""
        with self._lock:
            row = self._conn().execute(
                "SELECT COUNT(*) FROM semantic_entries"
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        """Close the store connection; repeated calls are safe."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


def utc_now_iso() -> str:
    """Current UTC time as a sortable ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
