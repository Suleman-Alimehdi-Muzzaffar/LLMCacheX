"""Tests for retry with exponential backoff."""

import time

import pytest

from llmcachex import cached_call
from llmcachex.resilience import run_with_retry


def test_retries_zero_means_single_attempt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=0)
    def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        always_fails()
    assert attempts == 1


def test_retry_count_matches_total_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=2, backoff_factor=0)
    def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        always_fails()
    assert attempts == 3  # initial attempt + 2 retries


def test_eventual_success_is_returned_and_cached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=3, backoff_factor=0)
    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        return "recovered"

    assert flaky() == "recovered"
    assert attempts == 3
    # Successful result is cached; no further executions.
    assert flaky() == "recovered"
    assert attempts == 3


def test_failure_preserves_exception_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call(retries=1, backoff_factor=0)
    def fails():
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        fails()


def test_failed_executions_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=0)
    def fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        fails()
    with pytest.raises(RuntimeError):
        fails()
    assert attempts == 2


def test_backoff_zero_keeps_suite_fast():
    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("x")
        return "ok"

    started = time.monotonic()
    assert run_with_retry(flaky, retries=1, backoff_factor=0) == "ok"
    assert time.monotonic() - started < 5


def test_exponential_backoff_delays_grow(monkeypatch):
    delays = []
    monkeypatch.setattr("llmcachex.resilience.retry.time.sleep", delays.append)
    attempts = 0

    def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        run_with_retry(always_fails, retries=3, backoff_factor=0.5)
    assert attempts == 4
    assert delays == [0.5, 1.0, 2.0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retries": -1},
        {"retries": 1.5},
        {"backoff_factor": -0.1},
    ],
)
def test_invalid_retry_config_rejected(kwargs):
    with pytest.raises(ValueError):
        cached_call(**kwargs)
    with pytest.raises(ValueError):
        run_with_retry(lambda: None, **kwargs)


@pytest.mark.parametrize("retries,expected_attempts", [(1, 2), (3, 4)])
def test_attempt_counts_for_remaining_retry_values(
    tmp_path, monkeypatch, retries, expected_attempts
):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=retries, backoff_factor=0)
    def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        always_fails()
    assert attempts == expected_attempts


def test_immediate_success_returns_without_sleeping():
    started = time.monotonic()
    assert run_with_retry(lambda: "ok", retries=3, backoff_factor=0) == "ok"
    assert time.monotonic() - started < 5


def test_on_retry_observer_called_once_per_retry():
    observed: list[str] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("x")
        return "ok"

    result = run_with_retry(
        flaky,
        retries=3,
        backoff_factor=0,
        on_retry=lambda exc: observed.append(type(exc).__name__),
    )
    assert result == "ok"
    assert observed == ["ConnectionError", "ConnectionError"]


def test_on_retry_not_called_when_no_retry_happens():
    observed: list[int] = []
    assert (
        run_with_retry(
            lambda: 1,
            retries=5,
            backoff_factor=0,
            on_retry=lambda exc: observed.append(1),
        )
        == 1
    )
    assert observed == []


def test_retry_then_cached_success_second_call_short_circuits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call(retries=1, backoff_factor=0)
    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("transient")
        return "done"

    assert flaky() == "done"
    assert flaky() == "done"  # cached: no more attempts
    assert attempts == 2
