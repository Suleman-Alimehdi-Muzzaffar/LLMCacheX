"""Phase 7 tests for the analytics API endpoints (real data, no fakes)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from llmcachex import cached_call
from llmcachex.analytics import AnalyticsStore, PricingConfig, RequestEvent, EventType


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


def seed_activity(db_path, *, priced: bool = True):
    """Generate real library activity through the actual decorator."""
    kwargs: dict = {
        "cache_path": db_path,
        "provider": "acme",
        "model": "acme-large",
    }
    if priced:
        kwargs["pricing"] = PricingConfig.from_values("0.003", "0.006")
        kwargs["usage"] = lambda response: {
            "input_tokens": 1000,
            "output_tokens": 500,
        }

    @cached_call(**kwargs)
    def work(x):
        return {"value": x}

    work("seed")  # miss (+ usage/cost when priced)
    work("seed")  # hit (+ savings when a prior cost exists)
    work("other")  # second miss


# ---------------------------------------------------------------- summary ---


def test_summary_zero_state_before_any_activity(client):
    body = client.get("/api/analytics/summary").json()
    assert body["total_requests"] == 0
    assert body["cache_hits"] == 0
    assert body["hit_rate"] == 0.0
    assert body["retry_count"] == 0
    # Unknown values are null, not fake zeros.
    assert body["average_latency_ms"] is None
    assert body["total_tokens"] is None
    assert body["estimated_cost"] is None
    assert body["estimated_cost_saved"] is None


def test_summary_reflects_real_activity(client, cache_db):
    seed_activity(cache_db)
    body = client.get("/api/analytics/summary").json()
    assert body["total_requests"] == 3
    assert body["cache_hits"] == 1
    assert body["cache_misses"] == 2
    assert body["hit_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert body["successful_requests"] == 3
    assert body["failed_requests"] == 0
    assert body["retry_count"] == 0
    assert body["rate_limit_events"] == 0
    assert body["average_latency_ms"] is not None
    assert body["total_execution_time_ms"] > 0
    assert body["total_tokens"] == 3000  # 2 misses x 1500
    assert body["estimated_cost"] == pytest.approx(0.006 * 2)
    assert body["estimated_cost_saved"] == pytest.approx(0.006)


def test_summary_unknown_usage_without_pricing(client, cache_db):
    seed_activity(cache_db, priced=False)
    body = client.get("/api/analytics/summary").json()
    assert body["total_requests"] == 3
    assert body["total_tokens"] is None
    assert body["estimated_cost"] is None
    assert body["estimated_cost_saved"] is None


# ------------------------------------------------------------- timeseries ---


def test_timeseries_hourly_points(client, cache_db):
    seed_activity(cache_db)
    body = client.get("/api/analytics/timeseries").json()
    assert body["bucket"] == "hour"
    assert body["hours"] == 24
    assert body["count"] >= 1
    point = body["points"][-1]
    assert point["requests"] == 3
    assert point["cache_hits"] == 1
    assert point["cache_misses"] == 2
    assert point["average_latency_ms"] is not None
    assert point["estimated_cost"] == pytest.approx(0.012)
    assert point["timestamp"].endswith(":00:00+00:00")


def test_timeseries_accepts_day_bucket(client, cache_db):
    seed_activity(cache_db)
    body = client.get("/api/analytics/timeseries?bucket=day&hours=48").json()
    assert body["bucket"] == "day"
    assert body["count"] >= 1
    assert body["points"][0]["timestamp"].endswith("T00:00:00+00:00")


def test_timeseries_rejects_invalid_params(client):
    assert client.get("/api/analytics/timeseries?bucket=minute").status_code == 422
    assert client.get("/api/analytics/timeseries?hours=0").status_code == 422
    assert client.get("/api/analytics/timeseries?hours=99999").status_code == 422


# -------------------------------------------------------------- providers ---


def test_providers_grouped_from_real_data(client, cache_db):
    seed_activity(cache_db)
    body = client.get("/api/analytics/providers").json()
    assert isinstance(body, list)
    assert body[0]["provider"] == "acme"
    assert body[0]["requests"] == 3
    assert body[0]["cache_hits"] == 1
    assert body[0]["cache_misses"] == 2
    assert body[0]["total_tokens"] == 3000
    assert body[0]["estimated_cost"] == pytest.approx(0.012)


def test_providers_unknown_when_no_metadata(client, cache_db):
    @cached_call(cache_path=cache_db)
    def work(x):
        return x

    work("a")
    body = client.get("/api/analytics/providers").json()
    assert body[0]["provider"] == "unknown"  # real label, not invented
    assert body[0]["total_tokens"] is None
    assert body[0]["estimated_cost"] is None


# ------------------------------------------------------------------ usage ---


def test_usage_totals(client, cache_db):
    seed_activity(cache_db)
    body = client.get("/api/analytics/usage").json()
    assert body["input_tokens"] == 2000
    assert body["output_tokens"] == 1000
    assert body["total_tokens"] == 3000
    assert body["request_units"] is None
    assert body["estimated_cost"] == pytest.approx(0.012)
    assert body["estimated_cost_saved"] == pytest.approx(0.006)


def test_usage_null_when_unknown(client, cache_db):
    seed_activity(cache_db, priced=False)
    body = client.get("/api/analytics/usage").json()
    assert body["input_tokens"] is None
    assert body["output_tokens"] is None
    assert body["total_tokens"] is None
    assert body["estimated_cost"] is None
    assert body["estimated_cost_saved"] is None


# ------------------------------------------------------------------ clear ---


def test_clear_analytics_keeps_cache_and_metrics(client, cache_db):
    seed_activity(cache_db)
    entries_before = client.get("/api/cache").json()["count"]
    assert entries_before == 2

    response = client.delete("/api/analytics")
    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["events_cleared"] == 3  # 2 misses + 1 hit

    # Analytics history is gone...
    summary = client.get("/api/analytics/summary").json()
    assert summary["total_requests"] == 0
    assert summary["total_tokens"] is None
    # ...but the cache and Phase 6 counters are untouched.
    assert client.get("/api/cache").json()["count"] == entries_before
    assert client.get("/api/stats").json()["total_requests"] == 3
    # Clearing twice is safe.
    assert client.delete("/api/analytics").json()["events_cleared"] == 0


# -------------------------------------------------------------- security ---


def test_analytics_never_exposes_prompts_or_responses(client, cache_db):
    secret_prompt = "TOP-SECRET-PROMPT-XYZ"

    @cached_call(cache_path=cache_db)
    def work(prompt):
        return {"answer": "TOP-SECRET-RESPONSE-ABC"}

    work(secret_prompt)
    for path in (
        "/api/analytics/summary",
        "/api/analytics/timeseries",
        "/api/analytics/providers",
        "/api/analytics/usage",
    ):
        payload = client.get(path).text
        assert "TOP-SECRET" not in payload
    # Activity feed and cache listing stay metadata-only too.
    assert "TOP-SECRET" not in client.get("/api/activity").text
    assert "TOP-SECRET" not in client.get("/api/cache").text


def test_analytics_store_and_table_are_separate_from_cache(client, cache_db):
    seed_activity(cache_db)
    store = AnalyticsStore(cache_db)
    try:
        assert store.count() == 3
        # analytics_events rows exist alongside cache_entries; the cache
        # listing only exposes cache metadata (no analytics columns).
        entry = client.get("/api/cache").json()["entries"][0]
        assert set(entry) == {"request_hash", "created_at", "expires_at", "expired"}
    finally:
        store.close()


def test_event_recorded_directly_is_counted(client, cache_db):
    store = AnalyticsStore(cache_db)
    try:
        store.record(
            RequestEvent(
                event_type=EventType.CACHE_HIT,
                function_name="manual.entry",
                request_hash="manual-hash",
                cache_status="hit",
                success=True,
            )
        )
    finally:
        store.close()
    summary = client.get("/api/analytics/summary").json()
    assert summary["total_requests"] == 1
    assert summary["cache_hits"] == 1
