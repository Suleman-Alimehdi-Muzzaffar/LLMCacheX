"""Phase 6 tests for the local dashboard API (real data, no fakes)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.services import MetricsService
from llmcachex import cached_call
from llmcachex.metrics import MAX_EVENTS, MetricsRecorder
from llmcachex.storage import SQLiteStorage


@pytest.fixture
def cache_db(tmp_path):
    """Path of a fresh temporary cache database."""
    return tmp_path / "cache.db"


@pytest.fixture
def client(cache_db):
    """TestClient bound to an app serving the temporary cache database."""
    app = create_app(cache_db=str(cache_db))
    with TestClient(app) as test_client:
        yield test_client
    app.state.service.close()


def seed_activity(db_path, hits=2, with_retry=True, with_rate_limit=True):
    """Generate real library activity against ``db_path``.

    Creates one cached function invocation pattern producing cache hits,
    a retry event and a rate-limit wait — all via the actual decorator.
    """
    attempts = {"count": 0}

    @cached_call(
        cache_path=db_path,
        retries=2,
        backoff_factor=0,
        rate_limit=1,
        rate_period=0.3,
    )
    def work(x):
        attempts["count"] += 1
        if with_retry and attempts["count"] == 1:
            raise ConnectionError("transient")
        return {"value": x}

    work("seed")  # miss (+ rate-limit slot consumption, maybe retry)
    work("seed")  # hit
    work("seed")  # hit
    if with_rate_limit:
        # Distinct argument: a second miss while the window (1/0.3s) is
        # still full forces an observable rate-limit wait.
        work("second")
    return attempts


# ------------------------------------------------------------------ health ---


def test_health_returns_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "service": "LLMCacheX",
        "cache_backend": "sqlite",
    }


def test_health_does_not_leak_internals(client):
    body = client.get("/api/health").text
    assert "Traceback" not in body
    assert "venv" not in body


# ------------------------------------------------------------------- stats ---


def test_stats_empty_cache_is_all_zero(client):
    body = client.get("/api/stats").json()
    assert body["total_requests"] == 0
    assert body["cache_hits"] == 0
    assert body["cache_misses"] == 0
    assert body["hit_rate"] == 0.0
    assert body["stored_entries"] == 0
    assert body["expired_entries"] == 0
    assert body["retry_count"] == 0
    assert body["rate_limit_events"] == 0
    assert body["successful_calls"] == 0
    assert body["failed_calls"] == 0
    assert body["avg_execution_latency_ms"] == 0.0
    assert body["latest_execution_latency_ms"] == 0.0


def test_stats_reflect_real_activity(cache_db, client):
    seed_activity(cache_db)
    body = client.get("/api/stats").json()

    assert body["total_requests"] >= 3
    assert body["cache_hits"] == 2  # two 'seed' repeats came from cache
    assert body["cache_misses"] >= 1
    # Consistency invariant of the transactional counters.
    assert body["total_requests"] == body["cache_hits"] + body["cache_misses"]
    assert body["hit_rate"] == pytest.approx(
        body["cache_hits"] / body["total_requests"], abs=1e-4
    )
    assert body["stored_entries"] == 2  # 'seed' and 'second' entries
    assert body["retry_count"] >= 1  # first attempt raised ConnectionError
    assert body["rate_limit_events"] >= 1  # second miss waited
    assert body["successful_calls"] >= 2
    assert body["avg_execution_latency_ms"] > 0
    assert body["latest_execution_latency_ms"] > 0


def test_stats_counts_failed_calls(cache_db, client):
    @cached_call(cache_path=cache_db, retries=0)
    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        always_fails()
    with pytest.raises(ValueError):
        always_fails()

    body = client.get("/api/stats").json()
    assert body["failed_calls"] == 2
    assert body["successful_calls"] == 0
    assert body["stored_entries"] == 0  # failures are never cached
    assert body["total_requests"] == body["cache_misses"]


def test_stats_counts_expired_entries(cache_db, client):
    from datetime import datetime, timedelta, timezone

    storage = SQLiteStorage(cache_db)
    storage.set(
        "expired-hash",
        {"x": 1},
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    storage.set("live-hash", {"x": 2})
    storage.close()

    body = client.get("/api/stats").json()
    assert body["stored_entries"] == 2
    assert body["expired_entries"] == 1


# ------------------------------------------------------------------- cache ---


def test_cache_list_empty(client):
    body = client.get("/api/cache").json()
    assert body == {"entries": [], "count": 0}


def test_cache_list_returns_metadata_only_not_payloads(cache_db, client):
    storage = SQLiteStorage(cache_db)
    storage.set("hash-1", {"secret_prompt": "TOP-SECRET-VALUE"})
    storage.close()

    response = client.get("/api/cache")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    entry = body["entries"][0]
    assert set(entry) == {"request_hash", "created_at", "expires_at", "expired"}
    assert entry["request_hash"] == "hash-1"
    assert entry["expires_at"] is None
    assert entry["expired"] is False
    # The raw response content must never appear in the payload.
    assert "TOP-SECRET-VALUE" not in response.text
    assert "response_json" not in response.text


def test_cache_list_flags_expired_entries(cache_db, client):
    from datetime import datetime, timedelta, timezone

    storage = SQLiteStorage(cache_db)
    storage.set(
        "old",
        1,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    storage.set("fresh", 2)
    storage.close()

    body = client.get("/api/cache").json()
    statuses = {e["request_hash"]: e["expired"] for e in body["entries"]}
    assert statuses == {"old": True, "fresh": False}


def test_cache_list_is_newest_first(cache_db, client):
    storage = SQLiteStorage(cache_db)
    for i in range(3):
        storage.set(f"hash-{i}", i)
    storage.close()

    body = client.get("/api/cache").json()
    assert [e["request_hash"] for e in body["entries"]] == [
        "hash-2",
        "hash-1",
        "hash-0",
    ]


def test_delete_cache_clears_entries_and_keeps_metrics(cache_db, client):
    seed_activity(cache_db, with_rate_limit=False)
    before = client.get("/api/stats").json()

    response = client.delete("/api/cache")
    assert response.status_code == 200
    assert response.json() == {"message": "Cache cleared", "cleared": True}

    assert client.get("/api/cache").json() == {"entries": [], "count": 0}
    after = client.get("/api/stats").json()
    assert after["stored_entries"] == 0
    # Runtime counters describe history, not cache contents: they stay.
    assert after["total_requests"] == before["total_requests"]
    assert after["cache_hits"] == before["cache_hits"]


# --------------------------------------------------------------- activity ---


def test_activity_empty(client):
    body = client.get("/api/activity").json()
    assert body == {"events": [], "count": 0}


def test_activity_returns_real_bounded_events(cache_db, client):
    seed_activity(cache_db)
    body = client.get("/api/activity").json()

    types = {event["type"] for event in body["events"]}
    assert {"cache_hit", "cache_miss", "function_execution"} <= types
    assert body["count"] == len(body["events"])
    # Newest first.
    timestamps = [event["timestamp"] for event in body["events"]]
    assert timestamps == sorted(timestamps, reverse=True)
    # Metadata only: no payloads/keys.
    for event in body["events"]:
        assert set(event) == {"type", "function", "timestamp", "latency_ms"}


def test_activity_limit_is_respected(cache_db, client):
    recorder = MetricsRecorder(cache_db)
    for _ in range(10):
        recorder.record_cache_hit("f", 1.0)
    recorder.close()

    body = client.get("/api/activity", params={"limit": 3}).json()
    assert body["count"] == 3


def test_activity_history_is_bounded(cache_db, client):
    recorder = MetricsRecorder(cache_db)
    for _ in range(MAX_EVENTS + 50):
        recorder.record_cache_hit("fn", 0.1)
    recorder.close()

    body = client.get("/api/activity", params={"limit": 200}).json()
    assert body["count"] <= MAX_EVENTS


@pytest.mark.parametrize("limit", [0, -5, 201])
def test_activity_invalid_limit_rejected(client, limit):
    response = client.get("/api/activity", params={"limit": limit})
    assert response.status_code == 422


# -------------------------------------------------------------------- CORS ---


def test_cors_allows_local_frontend_origin(client):
    response = client.get(
        "/api/health", headers={"Origin": "http://localhost:5173"}
    )
    assert response.headers.get("access-control-allow-origin") == (
        "http://localhost:5173"
    )


def test_cors_blocks_unknown_origins(client):
    response = client.get(
        "/api/health", headers={"Origin": "http://evil.example"}
    )
    assert response.headers.get("access-control-allow-origin") is None


# --------------------------------------------------------------- service ----


def test_service_default_path_env(monkeypatch):
    from api.services import get_cache_db_path

    monkeypatch.delenv("LLMCACHEX_CACHE_DB", raising=False)
    assert get_cache_db_path() == "llmcachex.db"
    monkeypatch.setenv("LLMCACHEX_CACHE_DB", "custom.db")
    assert get_cache_db_path() == "custom.db"


def test_app_startup_opens_database_lazily(cache_db):
    """Importing/creating the app must not touch the filesystem.

    The database is opened when the server (TestClient) starts and the
    connection is closed again on shutdown.
    """
    app = create_app(cache_db=str(cache_db))
    assert not cache_db.exists()  # nothing opened yet

    with TestClient(app) as test_client:
        assert cache_db.exists()  # opened at startup
        assert test_client.get("/api/stats").status_code == 200

    assert app.state.service._storage._connection is None  # closed


def test_clear_twice_is_safe(client):
    client.delete("/api/cache")
    assert client.delete("/api/cache").json()["cleared"] is True


def test_service_facade_direct_usage(cache_db):
    service = MetricsService(cache_db)
    try:
        assert service.stats().total_requests == 0
        assert service.cache_entries().count == 0
        assert service.activity(limit=10).count == 0
        assert service.clear_cache().cleared is True
        assert service.cache_db == cache_db
    finally:
        service.close()


# ------------------------------------------------------------ error surface ---


def test_broken_cache_table_returns_503_not_traceback(client, cache_db):
    """Unreadable cache storage becomes a clean 503 (documented contract).

    The table is dropped after startup to simulate database failure
    while the server is running; the response must carry the clean
    detail message and no traceback or filesystem paths.
    """
    with sqlite3.connect(cache_db) as con:
        con.execute("DROP TABLE cache_entries")
        con.commit()

    response = client.get("/api/cache")
    assert response.status_code == 503
    assert response.json() == {"detail": "Cache database error"}
    assert "Traceback" not in response.text
    assert str(cache_db) not in response.text
