"""Redis-backed response storage for LLMCacheX.

Values are JSON documents (never pickle) under a dedicated key prefix::

    llmcachex:cache:<request_hash> -> {
        "response": ...,
        "created_at": "2026-...T...+00:00",
        "expires_at": "2026-...T...+00:00" | null,
        "request_hash": "<hash>"
    }

TTL uses native Redis expiration; the stored document additionally
carries ``created_at``/``expires_at`` so cache inspection behaves like
the SQLite backend. Listing and clearing use ``SCAN`` (never ``KEYS``)
and ``clear()`` only removes keys inside the configured namespace —
never ``FLUSHDB``/``FLUSHALL``.

Requires the optional ``redis`` dependency
(``pip install "llmcachex[redis]"``). URLs, errors and logs never carry
passwords: use :func:`redact_redis_url` before displaying any URL.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .sqlite import CacheEntry, CacheEntryMeta

__all__ = [
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_REDIS_URL",
    "RedisStorage",
    "RedisStorageError",
    "redact_redis_url",
]

#: Local-development default (Docker: ``llmcachex-redis`` on 6379).
DEFAULT_REDIS_URL: str = "redis://localhost:6379/0"

#: Dedicated namespace; raw request hashes are never used as bare keys.
DEFAULT_KEY_PREFIX: str = "llmcachex:cache:"

#: Batch size for namespace cleanup deletes.
_CLEAR_BATCH_SIZE = 500

#: Hint for SCAN cursor sizing (a hint only — SCAN may return any count).
_SCAN_COUNT_HINT = 500


class RedisStorageError(Exception):
    """A Redis cache operation failed (unavailable, refused, corrupt).

    Messages are actionable and never contain passwords or API keys.
    """


def redact_redis_url(url: str) -> str:
    """Return ``url`` with any password replaced by ``***``.

    Safe to display in errors, logs and UIs.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "redis://<invalid-url>"
    if parts.password:
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{host}"
        else:
            netloc = f"***@{host}"
        return urlunsplit(parts._replace(netloc=netloc))
    return url


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


class RedisStorage:
    """Persist JSON-serializable responses in Redis, keyed by request hash.

    Args:
        redis_url: Redis server URL, e.g. ``"redis://localhost:6379/0"``.
            Authenticated URLs (``redis://user:pass@host:port/db``) are
            supported; the password is never logged or returned.
        key_prefix: Namespace for cache keys (a trailing ``":"`` is
            added when missing). Clearing only touches this namespace.
        client: Optional pre-built client (or test double). When given,
            no connection is opened and ``redis_url`` is only validated.
        socket_timeout: Socket timeout in seconds for Redis operations.

    Raises:
        ValueError: Invalid URL or key prefix, or a non-JSON-serializable
            response passed to :meth:`set`.
        RedisStorageError: The ``redis`` package is missing, the server
            is unreachable, a command fails, or a stored value is
            corrupt. Never falls back to another backend.
    """

    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        *,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        client: Any | None = None,
        socket_timeout: float = 5.0,
    ) -> None:
        if not isinstance(redis_url, str) or not redis_url.strip():
            raise ValueError(
                "redis_url must be a non-empty string, e.g. "
                f"{DEFAULT_REDIS_URL!r}."
            )
        scheme = urlsplit(redis_url.strip()).scheme.lower()
        if scheme not in ("redis", "rediss", "unix"):
            raise ValueError(
                f"redis_url must use redis://, rediss:// or unix://, "
                f"got scheme {scheme!r} in {redact_redis_url(redis_url)!r}."
            )
        if not isinstance(key_prefix, str) or not key_prefix.strip():
            raise ValueError("key_prefix must be a non-empty string.")
        if not key_prefix.endswith(":"):
            key_prefix = f"{key_prefix}:"
        self._redis_url = redis_url.strip()
        self._prefix = key_prefix
        self._lock = threading.RLock()
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            try:
                import redis
            except ImportError as exc:
                raise RedisStorageError(
                    'The "redis" package is required for strategy="redis". '
                    'Install it with pip install "llmcachex[redis]".'
                ) from exc
            self._client = redis.Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=socket_timeout,
                socket_timeout=socket_timeout,
            )
            self._owns_client = True
        # Fail fast with an actionable error — never silently degrade.
        try:
            self._client.ping()
        except RedisStorageError:
            raise
        except Exception as exc:
            raise RedisStorageError(
                "Redis backend is unavailable at "
                f"{redact_redis_url(self._redis_url)!r}. Ensure Redis is "
                "running and the URL is correct."
            ) from exc

    # ------------------------------------------------------------- helpers ---

    def _key(self, request_hash: str) -> str:
        """Namespaced Redis key for a request hash (hash only, no secrets)."""
        return f"{self._prefix}{request_hash}"

    def _live_client(self) -> Any:
        """Return the live client or raise if the storage is closed."""
        client = self._client
        if client is None:
            raise RedisStorageError("RedisStorage connection is closed.")
        return client

    @property
    def key_prefix(self) -> str:
        """Configured key namespace (read-only introspection, no secrets)."""
        return self._prefix

    def _command_error(self, operation: str, exc: Exception) -> RedisStorageError:
        """Wrap a Redis command failure with a credential-free message."""
        return RedisStorageError(
            f"Redis {operation} failed at "
            f"{redact_redis_url(self._redis_url)!r}. Ensure Redis is "
            f"running and reachable: {exc}"
        )

    # ---------------------------------------------------------- operations ---

    def get_entry(self, request_hash: str) -> CacheEntry | None:
        """Return the valid entry for ``request_hash``, or None on miss.

        Expired entries behave as cache misses (they may already have
        been removed by native Redis expiration). Corrupted values raise
        :class:`RedisStorageError` instead of a silent miss.
        """
        key = self._key(request_hash)
        try:
            raw = self._live_client().get(key)
        except RedisStorageError:
            raise
        except Exception as exc:
            raise self._command_error("read", exc) from exc
        if raw is None:
            return None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RedisStorageError(
                f"Corrupted cache value for request {request_hash!r}: "
                "stored JSON is invalid. The entry was not deleted."
            ) from exc
        if not isinstance(document, dict) or "response" not in document:
            raise RedisStorageError(
                f"Corrupted cache value for request {request_hash!r}: "
                "stored document has an unexpected shape. "
                "The entry was not deleted."
            )
        expires_at = _parse_expires_at(document.get("expires_at"))
        if expires_at is not None and expires_at <= _utcnow():
            return None
        return CacheEntry(
            response=document["response"], expires_at=expires_at
        )

    def get(self, request_hash: str) -> object | None:
        """Return the cached Python object for ``request_hash``, or None."""
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
                ``None`` (default) means the entry never expires. Naive
                datetimes are assumed to be UTC.

        Raises:
            TypeError: If ``expires_at`` is neither ``None`` nor a datetime.
            ValueError: If ``response`` is not JSON-serializable.
        """
        if expires_at is not None and not isinstance(expires_at, datetime):
            raise TypeError(
                "expires_at must be a datetime or None, got "
                f"{type(expires_at).__name__!r}."
            )
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        try:
            payload = json.dumps(
                {
                    "response": response,
                    "created_at": _utcnow().isoformat(),
                    "expires_at": (
                        expires_at.isoformat() if expires_at else None
                    ),
                    "request_hash": request_hash,
                },
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Response for request {request_hash!r} is not JSON "
                f"serializable and therefore cannot be cached: {exc}"
            ) from exc
        ttl_seconds: int | None = None
        if expires_at is not None:
            ttl_seconds = int((expires_at - _utcnow()).total_seconds())
        key = self._key(request_hash)
        client = self._live_client()
        with self._lock:
            try:
                if ttl_seconds is not None and ttl_seconds > 0:
                    client.set(key, payload, ex=ttl_seconds)
                else:
                    # No (or already past) TTL: persistent key; expiry is
                    # enforced at read time from the stored metadata,
                    # matching the SQLite backend's behavior.
                    client.set(key, payload)
            except RedisStorageError:
                raise
            except Exception as exc:
                raise self._command_error("write", exc) from exc

    def exists(self, request_hash: str) -> bool:
        """Return True if a valid (unexpired) entry for ``request_hash`` exists."""
        key = self._key(request_hash)
        try:
            present = self._live_client().exists(key)
        except RedisStorageError:
            raise
        except Exception as exc:
            raise self._command_error("read", exc) from exc
        if not present:
            return False
        return self.get_entry(request_hash) is not None

    def list_entries(self) -> list[CacheEntryMeta]:
        """Return metadata for every stored row, newest first.

        Only keys inside the configured namespace are scanned (``SCAN``,
        never ``KEYS``); response payloads are intentionally excluded.
        """
        now = _utcnow()
        metas: list[tuple[str, CacheEntryMeta]] = []
        client = self._live_client()
        try:
            keys = list(
                client.scan_iter(
                    match=f"{self._prefix}*", count=_SCAN_COUNT_HINT
                )
            )
        except RedisStorageError:
            raise
        except Exception as exc:
            raise self._command_error("scan", exc) from exc
        for key in keys:
            name = key if isinstance(key, str) else key.decode("utf-8", "replace")
            request_hash = name[len(self._prefix):]
            try:
                raw = client.get(key)
            except RedisStorageError:
                raise
            except Exception as exc:
                raise self._command_error("read", exc) from exc
            if raw is None:
                continue  # expired between SCAN and GET
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RedisStorageError(
                    f"Corrupted cache value for request {request_hash!r}: "
                    "stored JSON is invalid."
                ) from exc
            if not isinstance(document, dict) or "response" not in document:
                raise RedisStorageError(
                    f"Corrupted cache value for request {request_hash!r}: "
                    "stored document has an unexpected shape."
                )
            created_at = document.get("created_at")
            expires_raw = document.get("expires_at")
            parsed = _parse_expires_at(expires_raw)
            metas.append(
                (
                    created_at if isinstance(created_at, str) else "",
                    CacheEntryMeta(
                        request_hash=request_hash,
                        created_at=(
                            created_at if isinstance(created_at, str) else ""
                        ),
                        expires_at=(
                            expires_raw if isinstance(expires_raw, str) else None
                        ),
                        expired=parsed is not None and parsed <= now,
                    ),
                )
            )
        metas.sort(key=lambda item: item[0], reverse=True)
        return [meta for _, meta in metas]

    def clear(self) -> int:
        """Remove all LLMCacheX entries in the namespace (SCAN + delete).

        Keys outside the configured prefix are never touched — no
        ``FLUSHDB``/``FLUSHALL``. Returns the number of keys removed.
        """
        with self._lock:
            client = self._live_client()
            try:
                keys = list(
                    client.scan_iter(
                        match=f"{self._prefix}*", count=_SCAN_COUNT_HINT
                    )
                )
            except RedisStorageError:
                raise
            except Exception as exc:
                raise self._command_error("scan", exc) from exc
            removed = 0
            for start in range(0, len(keys), _CLEAR_BATCH_SIZE):
                batch = keys[start:start + _CLEAR_BATCH_SIZE]
                try:
                    removed += int(client.delete(*batch))
                except RedisStorageError:
                    raise
                except Exception as exc:
                    raise self._command_error("delete", exc) from exc
            return removed

    def close(self) -> None:
        """Disconnect the client when owned; repeated calls are safe."""
        with self._lock:
            client, self._client = self._client, None
            if client is not None and self._owns_client:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    # ----------------------------------------------------------- debugging ---

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(url="
            f"{redact_redis_url(self._redis_url)!r}, "
            f"key_prefix={self._prefix!r})"
        )
