"""Phase 7 tests: analytics events, aggregations, retention, decorator
integration. Deterministic; no network access, no fabricated data."""

from __future__ import annotations

from decimal import Decimal

import pytest

from llmcachex import cached_call
from llmcachex.analytics import (
    DEFAULT_MAX_EVENTS,
    EVENT_TYPES,
    AnalyticsStore,
    EventType,
    PricingConfig,
    RequestEvent,
    Usage,
)
from llmcachex.metrics import MAX_EVENTS, MetricsRecorder


def make_event(event_type: str = EventType.CACHE_MISS, **kwargs) -> RequestEvent:
    """Build a valid event with overridable fields."""
    defaults = {
        "function_name": "app.generate",
        "request_hash": "hash-1",
        "cache_status": "miss" if event_type == EventType.CACHE_MISS else None,
        "success": True if event_type in ("cache_hit", "cache_miss") else None,
    }
    defaults.update(kwargs)
    return RequestEvent(event_type=event_type, **defaults)


# ------------------------------------------------------------ event model ---


def test_event_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unknown event type"):
        RequestEvent(event_type="invented", function_name="f", request_hash="h")


def test_event_rejects_bad_cache_status():
    with pytest.raises(ValueError, match="cache_status"):
        RequestEvent(
            event_type=EventType.CACHE_HIT,
            function_name="f",
            request_hash="h",
            cache_status="maybe",
        )


def test_event_vocabulary_is_small_and_documented():
    assert EVENT_TYPES == {
        "cache_hit",
        "cache_miss",
        "retry",
        "rate_limit_wait",
    }


# ----------------------------------------------------------------- store ---


def test_empty_summary_is_zero_and_unknown_is_null(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        summary = store.summary()
        assert summary.total_requests == 0
        assert summary.cache_hits == 0
        assert summary.cache_misses == 0
        assert summary.hit_rate == 0.0
        assert summary.successful_requests == 0
        assert summary.failed_requests == 0
        assert summary.retry_count == 0
        assert summary.rate_limit_events == 0
        # Unknown, not zero:
        assert summary.average_latency_ms is None
        assert summary.total_tokens is None
        assert summary.estimated_cost is None
        assert summary.estimated_cost_saved is None
    finally:
        store.close()


def test_record_and_summary_counts(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        store.record(
            make_event(
                EventType.CACHE_MISS, request_hash="h1", latency_ms=10.0
            )
        )
        store.record(
            make_event(
                EventType.CACHE_HIT,
                request_hash="h2",
                cache_status="hit",
                latency_ms=0.2,
            )
        )
        store.record(RequestEvent(event_type=EventType.RETRY, function_name="f", request_hash="h1"))
        store.record(
            RequestEvent(
                event_type=EventType.RATE_LIMIT_WAIT,
                function_name="f",
                request_hash="h1",
            )
        )
        summary = store.summary()
        assert summary.total_requests == 2
        assert summary.cache_hits == 1
        assert summary.cache_misses == 1
        assert summary.hit_rate == 0.5
        assert summary.successful_requests == 2
        assert summary.failed_requests == 0
        assert summary.retry_count == 1
        assert summary.rate_limit_events == 1
        assert summary.average_latency_ms == 10.0  # executions only
        assert summary.total_execution_time_ms == 10.0
        assert store.count() == 4
    finally:
        store.close()


def test_usage_and_cost_totals_and_exact_decimal_roundtrip(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        cost = Decimal("0.0000123456789012345")
        store.record(
            make_event(
                input_tokens=1200,
                output_tokens=300,
                total_tokens=1500,
                estimated_cost=cost,
            )
        )
        store.record(
            make_event(
                event_type=EventType.CACHE_HIT,
                request_hash="hash-2",
                cache_status="hit",
                estimated_cost_saved=cost,
            )
        )
        totals = store.usage_totals()
        assert totals.input_tokens == 1200
        assert totals.output_tokens == 300
        assert totals.total_tokens == 1500
        assert totals.estimated_cost == cost  # exact, not float-rounded
        assert totals.estimated_cost_saved == cost
        summary = store.summary()
        assert summary.estimated_cost == cost
        assert summary.estimated_cost_saved == cost
    finally:
        store.close()


def test_tokens_stay_unknown_when_absent(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        store.record(make_event())
        totals = store.usage_totals()
        assert totals.input_tokens is None
        assert totals.output_tokens is None
        assert totals.total_tokens is None
        assert totals.request_units is None
        assert totals.estimated_cost is None
    finally:
        store.close()


def test_retention_bounds_stored_events(tmp_path):
    db = tmp_path / "a.db"
    store = AnalyticsStore(db, max_events=5)
    try:
        for i in range(12):
            store.record(make_event(request_hash=f"h{i}"))
        assert store.count() == 5
        summary = store.summary()
        assert summary.total_requests == 5  # newest kept
    finally:
        store.close()

    # Default retention constant is documented and configurable.
    assert DEFAULT_MAX_EVENTS == 1000
    with pytest.raises(ValueError, match="max_events"):
        AnalyticsStore(tmp_path / "b.db", max_events=0)
    with pytest.raises(ValueError, match="max_events"):
        AnalyticsStore(tmp_path / "c.db", max_events="many")


def test_activity_history_bound_is_configurable(tmp_path):
    db = tmp_path / "m.db"
    recorder = MetricsRecorder(db, max_events=3)
    try:
        for i in range(7):
            recorder.record_cache_hit("f", 1.0)
        assert len(recorder.recent_events(limit=50)) == 3
        assert MAX_EVENTS >= 1  # default constant still documented
    finally:
        recorder.close()


def test_clear_analytics_returns_count_and_keeps_cache(tmp_path):
    db = tmp_path / "cache.db"
    store = AnalyticsStore(db)
    other = AnalyticsStore(db)  # second connection, same file
    try:
        store.record(make_event())
        store.record(make_event(request_hash="hash-2"))
        assert store.clear() == 2
        assert store.count() == 0
        assert store.summary().total_requests == 0
        assert other.count() == 0
        assert store.clear() == 0  # idempotent
    finally:
        store.close()
        other.close()


def test_timeseries_buckets_and_nulls(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        store.record(
            make_event(
                EventType.CACHE_MISS, request_hash="h1", latency_ms=5.0,
                estimated_cost=Decimal("0.01"),
            )
        )
        store.record(
            make_event(
                event_type=EventType.CACHE_HIT,
                request_hash="h2",
                cache_status="hit",
                latency_ms=0.1,
            )
        )
        points = store.timeseries(bucket="hour", hours=24)
        assert len(points) == 1
        point = points[0]
        assert point.requests == 2
        assert point.cache_hits == 1
        assert point.cache_misses == 1
        assert point.average_latency_ms == 5.0  # execution latency only
        assert point.estimated_cost == Decimal("0.01")
        assert point.timestamp.endswith(":00:00+00:00")

        day_points = store.timeseries(bucket="day", hours=24)
        assert len(day_points) == 1
        assert day_points[0].timestamp.endswith("T00:00:00+00:00")

        with pytest.raises(ValueError, match="bucket"):
            store.timeseries(bucket="minute")
        with pytest.raises(ValueError, match="hours"):
            store.timeseries(hours=0)
    finally:
        store.close()


def test_timeseries_bucket_without_executions_has_null_latency(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        store.record(
            make_event(
                event_type=EventType.CACHE_HIT,
                request_hash="h1",
                cache_status="hit",
                latency_ms=0.1,
            )
        )
        point = store.timeseries(hours=1)[0]
        assert point.requests == 1
        assert point.average_latency_ms is None  # no executions
        assert point.estimated_cost is None  # no known cost
    finally:
        store.close()


def test_provider_grouping_unknown_when_unset(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        store.record(make_event(provider="acme", request_hash="h1"))
        store.record(make_event(provider="acme", request_hash="h2"))
        store.record(
            make_event(event_type=EventType.CACHE_HIT, request_hash="h3",
                       cache_status="hit")
        )
        providers = store.providers()
        by_name = {entry.provider: entry for entry in providers}
        assert by_name["acme"].requests == 2
        assert by_name["unknown"].requests == 1
        assert by_name["acme"].total_tokens is None  # no tokens recorded
        # Sorted by request count, descending.
        assert providers[0].provider == "acme"
    finally:
        store.close()


def test_resolve_prior_context_for_cache_savings(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    try:
        assert store.resolve_prior_context("h1") is None
        store.record(
            make_event(
                provider="acme",
                request_hash="h1",
                estimated_cost=Decimal("0.02"),
            )
        )
        prior = store.resolve_prior_context("h1")
        assert prior is not None
        assert prior.provider == "acme"
        assert prior.estimated_cost == Decimal("0.02")
        assert store.resolve_prior_context("other") is None
    finally:
        store.close()


# ------------------------------------------------- decorator integration ---


def test_decorator_records_hit_miss_and_execution(tmp_path):
    db = tmp_path / "cache.db"
    calls = {"n": 0}

    @cached_call(cache_path=db)
    def generate(value):
        calls["n"] += 1
        return {"value": value}

    generate("a")  # miss + execution success
    generate("a")  # hit
    assert calls["n"] == 1

    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 2
        assert summary.cache_misses == 1
        assert summary.cache_hits == 1
        assert summary.successful_requests == 2
        assert summary.failed_requests == 0
        assert summary.average_latency_ms is not None
        # No provider/usage/pricing configured -> unknown, not zero.
        assert summary.total_tokens is None
        assert summary.estimated_cost is None
        assert summary.estimated_cost_saved is None
        assert store.providers()[0].provider == "unknown"
    finally:
        store.close()


def test_decorator_records_execution_failure(tmp_path):
    db = tmp_path / "cache.db"

    @cached_call(cache_path=db, retries=0)
    def broken(value):
        raise ValueError("always fails")

    with pytest.raises(ValueError):
        broken("x")
    with pytest.raises(ValueError):
        broken("x")  # failures are never cached

    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.failed_requests == 2
        assert summary.successful_requests == 0
        assert summary.total_requests == 2
        assert summary.cache_misses == 2
    finally:
        store.close()


def test_decorator_records_retries(tmp_path):
    db = tmp_path / "cache.db"
    attempts = {"n": 0}

    @cached_call(cache_path=db, retries=2, backoff_factor=0)
    def flaky(value):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    assert flaky("x") == "ok"
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.retry_count == 2
        assert summary.failed_requests == 0
        assert summary.successful_requests == 1
        assert summary.cache_misses == 1
        events = [row for row in store._load()]
        retry_rows = [e for e in events if e["event_type"] == "retry"]
        assert len(retry_rows) == 2
        assert retry_rows[0]["request_hash"] == retry_rows[1]["request_hash"]
    finally:
        store.close()


def test_decorator_records_rate_limit_wait(tmp_path):
    db = tmp_path / "cache.db"

    @cached_call(cache_path=db, rate_limit=1, rate_period=0.2)
    def slow(value):
        return value * 2

    slow("a")  # consumes the only slot
    assert slow("b") == "bb"  # distinct request -> must wait -> event
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.rate_limit_events == 1
        assert summary.total_requests == 2
    finally:
        store.close()


def test_decorator_usage_pricing_and_savings(tmp_path):
    db = tmp_path / "cache.db"
    pricing = PricingConfig.from_values(
        input_cost_per_1k_tokens="0.003",
        output_cost_per_1k_tokens="0.006",
    )
    calls = {"n": 0}

    @cached_call(
        cache_path=db,
        provider="acme",
        model="acme-large",
        pricing=pricing,
        usage=lambda response: {
            "input_tokens": 1000,
            "output_tokens": 500,
        },
    )
    def generate(prompt):
        calls["n"] += 1
        return {"text": "answer", "prompt": prompt}

    generate("hello")  # miss: cost = 0.003*1 + 0.006*0.5 = 0.006
    generate("hello")  # hit: savings = 0.006
    assert calls["n"] == 1

    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_tokens == 1500
        assert summary.estimated_cost == Decimal("0.006")
        assert summary.estimated_cost_saved == Decimal("0.006")
        provider_rows = store.providers()
        assert provider_rows[0].provider == "acme"
        assert provider_rows[0].requests == 2
        assert provider_rows[0].total_tokens == 1500
        assert provider_rows[0].estimated_cost == Decimal("0.006")
    finally:
        store.close()


def test_decorator_unknown_usage_when_extractor_returns_none(tmp_path):
    db = tmp_path / "cache.db"

    @cached_call(
        cache_path=db,
        provider="acme",
        pricing=PricingConfig.from_values("0.01", "0.02"),
        usage=lambda response: None,  # provider reported no usage
    )
    def generate(prompt):
        return {"text": "answer"}

    generate("hello")
    generate("hello")
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_tokens is None
        assert summary.estimated_cost is None
        assert summary.estimated_cost_saved is None  # savings unknown too
    finally:
        store.close()


def test_decorator_broken_usage_extractor_degrades_to_unknown(tmp_path):
    db = tmp_path / "cache.db"

    @cached_call(
        cache_path=db,
        usage=lambda response: {"input_tokens": -5},  # invalid -> ValueError
    )
    def generate(prompt):
        return {"text": "answer"}

    assert generate("hello") == {"text": "answer"}  # call still works
    store = AnalyticsStore(db)
    try:
        assert store.summary().total_tokens is None
    finally:
        store.close()


def test_decorator_rejects_invalid_provider_argument(tmp_path):
    with pytest.raises(ValueError, match="provider"):

        @cached_call(cache_path=tmp_path / "x.db", provider=123)
        def generate(prompt):
            return prompt

    with pytest.raises(ValueError, match="non-empty"):

        @cached_call(cache_path=tmp_path / "y.db", provider="   ")
        def other(prompt):
            return prompt


def test_hit_without_prior_execution_has_unknown_savings(tmp_path):
    db = tmp_path / "cache.db"

    @cached_call(cache_path=db, provider="acme", model="m1")
    def generate(prompt):
        return {"text": "answer"}

    generate("a")
    # Simulate lost analytics history: clear, then hit -> savings unknown.
    store = AnalyticsStore(db)
    store.clear()
    store.close()
    generate("a")
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 1
        assert summary.cache_hits == 1
        assert summary.estimated_cost_saved is None  # not fabricated
    finally:
        store.close()


def test_usage_derives_total_from_input_and_output():
    usage = Usage(input_tokens=700, output_tokens=300)
    assert usage.total == 1000  # exact arithmetic on supplied values
    assert Usage(input_tokens=700).total is None  # cannot derive
    assert usage.has_token_data is True
    assert Usage().has_token_data is False
