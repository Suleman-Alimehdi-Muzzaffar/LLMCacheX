"""Select a cache backend by strategy name.

``"sqlite"`` (the default) stores responses in a local SQLite file;
``"redis"`` stores them in Redis under a dedicated key namespace while
metrics/analytics keep using the local SQLite file. Unknown strategies
raise :class:`ValueError` — never silently fall back to another backend.
"""

from __future__ import annotations

from os import PathLike
from typing import Any

from .base import CacheStorage
from .redis import DEFAULT_KEY_PREFIX, DEFAULT_REDIS_URL, RedisStorage
from .sqlite import SQLiteStorage

__all__ = [
    "STRATEGIES",
    "create_storage",
]

#: Supported ``strategy`` values (``"sqlite"`` is the default).
STRATEGIES: tuple[str, ...] = ("sqlite", "redis")


def create_storage(
    strategy: str = "sqlite",
    *,
    cache_path: str | PathLike[str] = "llmcachex.db",
    redis_url: str | None = None,
    redis_prefix: str | None = None,
    redis_client: Any | None = None,
) -> CacheStorage:
    """Build the cache backend for ``strategy``.

    Args:
        strategy: ``"sqlite"`` or ``"redis"``.
        cache_path: SQLite file used by ``"sqlite"`` (and, under
            ``"redis"``, for the local metrics/analytics file).
        redis_url: Redis URL for ``"redis"`` (defaults to
            ``redis://localhost:6379/0``); ignored for ``"sqlite"``.
        redis_prefix: Key namespace for ``"redis"``; ignored otherwise.
        redis_client: Optional pre-built client (or test double) for
            ``"redis"``; ignored otherwise.

    Raises:
        ValueError: Unknown strategy.
    """
    if strategy == "sqlite":
        return SQLiteStorage(cache_path)
    if strategy == "redis":
        return RedisStorage(
            redis_url if redis_url is not None else DEFAULT_REDIS_URL,
            key_prefix=(
                redis_prefix if redis_prefix is not None else DEFAULT_KEY_PREFIX
            ),
            client=redis_client,
        )
    raise ValueError(
        f"Unsupported cache strategy {strategy!r}. "
        f"Supported strategies: {', '.join(STRATEGIES)}."
    )
