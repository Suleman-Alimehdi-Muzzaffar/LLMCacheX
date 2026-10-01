"""Cache backend contract shared by all storage implementations.

A cache backend persists JSON-serializable responses keyed by the
deterministic request hash produced by the decorator. ``SQLiteStorage``
and ``RedisStorage`` both satisfy this protocol structurally; the
decorator and the dashboard API only rely on the operations defined
here, never on backend-specific details.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .sqlite import CacheEntry, CacheEntryMeta

__all__ = ["CacheStorage"]


@runtime_checkable
class CacheStorage(Protocol):
    """Minimal contract every LLMCacheX cache backend implements."""

    def get_entry(self, request_hash: str) -> CacheEntry | None:
        """Return the valid entry for ``request_hash``, else ``None``.

        Expired entries behave as cache misses (they may be retained or
        removed by the backend — callers must not rely on either).
        Corrupted values raise a clear error instead of a silent miss.
        """
        ...

    def get(self, request_hash: str) -> object | None:
        """Return the cached response object, or ``None`` on miss."""
        ...

    def set(
        self,
        request_hash: str,
        response: object,
        expires_at: datetime | None = None,
    ) -> None:
        """Store ``response`` under ``request_hash`` (upsert).

        ``expires_at`` is an optional timezone-aware UTC expiration;
        ``None`` means the entry never expires. The response must be
        JSON-serializable.
        """
        ...

    def exists(self, request_hash: str) -> bool:
        """Return True when a valid (unexpired) entry exists."""
        ...

    def list_entries(self) -> list[CacheEntryMeta]:
        """Return metadata for stored rows, newest first (no payloads)."""
        ...

    def clear(self) -> int:
        """Remove entries owned by LLMCacheX; return the removal count.

        Must never remove data outside the backend's own namespace.
        """
        ...

    def close(self) -> None:
        """Release backend resources; repeated calls are safe."""
        ...
