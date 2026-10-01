"""Phase 12 live Redis integration tests.

Run with a local Redis available (Docker)::

    docker run --name llmcachex-redis -p 6379:6379 -d redis:7-alpine
    pytest -m redis

Every test skips automatically when no server answers at
``redis://localhost:6379/0``. Each test uses a unique key prefix and
cleans up afterwards, so unrelated data is never touched.
"""

from __future__ import annotations

import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from llmcachex import cached_call
from llmcachex.analytics import AnalyticsStore
from llmcachex.storage import RedisStorage, RedisStorageError

pytestmark = pytest.mark.redis

LIVE_URL = "redis://localhost:6379/0"


def require_redis() -> None:
    """Skip the test when no Redis server is reachable."""
    try:
        sock = socket.create_connection(("localhost", 6379), timeout=1.0)
    except OSError:
        pytest.skip("Redis not available at localhost:6379")
    else:
        sock.close()


@pytest.fixture
def prefix() -> str:
    """Unique namespace per test (isolation + safe cleanup)."""
    return f"llmcachex:test:{uuid.uuid4().hex}:"


@pytest.fixture
def live_storage(prefix):
    require_redis()
    store = RedisStorage(redis_url=LIVE_URL, key_prefix=prefix)
    try:
        yield store
    finally:
        store.clear()
        store.close()


# ------------------------------------------------------------ operations ---


def test_live_set_get_roundtrip(live_storage):
    live_storage.set("k", {"answer": [1, 2, 3]})
    assert live_storage.get("k") == {"answer": [1, 2, 3]}
    assert live_storage.exists("k") is True
    assert live_storage.get("missing") is None


def test_live_ttl_expiration(live_storage):
    expires = datetime.now(timezone.utc) + timedelta(seconds=1)
    live_storage.set("ttl", {"v": 1}, expires_at=expires)
    assert live_storage.get("ttl") == {"v": 1}
    time.sleep(1.4)
    assert live_storage.get("ttl") is None
    assert live_storage.exists("ttl") is False


def test_live_persistent_key_has_no_expiry(live_storage, prefix):
    live_storage.set("keep", "x")
    import redis

    client = redis.Redis.from_url(LIVE_URL, decode_responses=True)
    try:
        assert client.ttl(f"{prefix}keep") == -1  # -1: no expiration set
    finally:
        client.close()


def test_live_clear_keeps_unrelated_keys(live_storage):
    import redis

    outsider = redis.Redis.from_url(LIVE_URL, decode_responses=True)
    try:
        live_storage.set("a", 1)
        live_storage.set("b", 2)
        outsider.set("other-app:untouchable", "keep-me")
        assert live_storage.clear() == 2
        assert outsider.get("other-app:untouchable") == "keep-me"
        assert live_storage.list_entries() == []
    finally:
        outsider.delete("other-app:untouchable")
        outsider.close()


def test_live_connection_error_is_actionable():
    with pytest.raises(RedisStorageError, match="unavailable") as exc_info:
        RedisStorage(redis_url="redis://localhost:6399/15")
    assert "6399" in str(exc_info.value)


# ------------------------------------------------------------ decorator ---


def test_live_decorator_miss_hit_and_analytics(tmp_path, prefix):
    require_redis()
    db = tmp_path / "redis_live.db"
    calls = []

    @cached_call(
        strategy="redis",
        redis_url=LIVE_URL,
        redis_prefix=prefix,
        cache_path=db,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    try:
        assert generate("live?") == {"answer": "live?"}
        assert generate("live?") == {"answer": "live?"}
        assert calls == ["live?"]
        store = AnalyticsStore(db)
        try:
            summary = store.summary()
            assert summary.total_requests == 2
            assert summary.cache_hits == 1
            assert summary.cache_misses == 1
            assert summary.successful_requests == 2
        finally:
            store.close()
    finally:
        RedisStorage(redis_url=LIVE_URL, key_prefix=prefix).clear()


# ------------------------------------------------------------------- api ---


def test_live_api_over_redis(tmp_path, prefix):
    require_redis()
    db = tmp_path / "redis_api.db"

    @cached_call(
        strategy="redis",
        redis_url=LIVE_URL,
        redis_prefix=prefix,
        cache_path=db,
    )
    def generate(prompt):
        return {"answer": prompt}

    app = create_app(
        cache_db=str(db),
        cache_strategy="redis",
        redis_url=LIVE_URL,
        redis_prefix=prefix,
    )
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").json()["cache_backend"] == "redis"
            assert generate("dash?") == {"answer": "dash?"}
            assert generate("dash?") == {"answer": "dash?"}
            cache = client.get("/api/cache").json()
            assert cache["count"] == 1
            stats = client.get("/api/stats").json()
            assert stats["stored_entries"] == 1
            assert stats["cache_hits"] == 1
            assert client.delete("/api/cache").json()["cleared"] is True
            assert client.get("/api/cache").json()["count"] == 0
    finally:
        app.state.service.close()
        RedisStorage(redis_url=LIVE_URL, key_prefix=prefix).clear()
