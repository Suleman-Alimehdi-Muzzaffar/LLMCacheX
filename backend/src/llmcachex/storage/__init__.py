"""Cache backends: SQLite (default) and Redis (optional extra)."""

from .base import CacheStorage
from .factory import STRATEGIES, create_storage
from .redis import (
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    RedisStorage,
    RedisStorageError,
    redact_redis_url,
)
from .sqlite import CacheEntry, CacheEntryMeta, SQLiteStorage

__all__ = [
    "CacheEntry",
    "CacheEntryMeta",
    "CacheStorage",
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_REDIS_URL",
    "RedisStorage",
    "RedisStorageError",
    "SQLiteStorage",
    "STRATEGIES",
    "create_storage",
    "redact_redis_url",
]
