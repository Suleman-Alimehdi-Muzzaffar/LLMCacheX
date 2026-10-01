"""Unit tests for the Phase 6 metrics recorder (real SQLite-backed state)."""

from __future__ import annotations

import sqlite3
import threading

from llmcachex.metrics import MAX_EVENTS, MetricsRecorder


def test_snapshot_defaults_to_zero_on_fresh_db(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        snapshot = recorder.snapshot()
        assert snapshot.total_requests == 0
        assert snapshot.cache_hits == 0
        assert snapshot.cache_misses == 0
        assert snapshot.retry_count == 0
        assert snapshot.rate_limit_events == 0
        assert snapshot.hit_rate == 0.0
        assert snapshot.avg_execution_latency_ms == 0.0
        assert recorder.recent_events() == []
    finally:
        recorder.close()


def test_hit_and_miss_counters_stay_consistent(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        for _ in range(3):
            recorder.record_cache_hit("f", 1.5)
        for _ in range(2):
            recorder.record_cache_miss("f")
        snapshot = recorder.snapshot()
        assert snapshot.total_requests == 5
        assert snapshot.cache_hits == 3
        assert snapshot.cache_misses == 2
        assert snapshot.hit_rate == 0.6
    finally:
        recorder.close()


def test_execution_latency_tracking(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        recorder.record_execution("f", 10.0)
        recorder.record_execution("f", 30.0)
        snapshot = recorder.snapshot()
        assert snapshot.successful_calls == 2
        assert snapshot.execution_count == 2
        assert snapshot.total_execution_time_ms == 40.0
        assert snapshot.avg_execution_latency_ms == 20.0
        assert snapshot.latest_execution_latency_ms == 30.0
    finally:
        recorder.close()


def test_failures_retries_and_rate_limit_counters(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        recorder.record_failure("f")
        recorder.record_retry("f")
        recorder.record_retry("f")
        recorder.record_rate_limit_wait("f")
        snapshot = recorder.snapshot()
        assert snapshot.failed_calls == 1
        assert snapshot.retry_count == 2
        assert snapshot.rate_limit_events == 1
    finally:
        recorder.close()


def test_events_are_bounded_to_max_events(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        for i in range(MAX_EVENTS + 60):
            recorder.record_cache_hit("f", float(i))
        assert len(recorder.recent_events(limit=MAX_EVENTS)) == MAX_EVENTS
        with sqlite3.connect(tmp_path / "metrics.db") as con:
            count = con.execute(
                "SELECT COUNT(*) FROM activity_events"
            ).fetchone()[0]
        assert count <= MAX_EVENTS
    finally:
        recorder.close()


def test_recent_events_newest_first_and_limit(tmp_path):
    recorder = MetricsRecorder(tmp_path / "metrics.db")
    try:
        recorder.record_cache_miss("first")
        recorder.record_cache_hit("second", 1.0)
        recorder.record_retry("third")
        events = recorder.recent_events(limit=2)
        assert [e.type for e in events] == ["retry", "cache_hit"]
        assert events[0].function == "third"
    finally:
        recorder.close()


def test_recorder_is_thread_safe(tmp_path):
    db = tmp_path / "metrics.db"
    recorder = MetricsRecorder(db)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(25):
                recorder.record_cache_hit("f", 1.0)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert recorder.snapshot().cache_hits == 100
    finally:
        recorder.close()


def test_metrics_persist_across_recorders(tmp_path):
    db = tmp_path / "metrics.db"
    first = MetricsRecorder(db)
    first.record_cache_hit("f", 2.0)
    first.record_cache_miss("f")
    first.close()

    second = MetricsRecorder(db)
    try:
        snapshot = second.snapshot()
        assert snapshot.total_requests == 2
        assert snapshot.cache_hits == 1
    finally:
        second.close()
