"""Tests for the process-local rate limiter."""

import threading
import time

import pytest

from llmcachex import cached_call
from llmcachex.resilience import RateLimiter


def test_requests_below_limit_execute_immediately():
    limiter = RateLimiter(5, 60)
    started = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - started < 5


def test_requests_beyond_limit_are_delayed():
    limiter = RateLimiter(2, 0.4)
    limiter.acquire()
    limiter.acquire()
    started = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - started >= 0.3


def test_limiter_is_thread_safe():
    limiter = RateLimiter(10, 60)
    errors = []

    def worker():
        try:
            for _ in range(5):
                limiter.acquire()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_rate_limit_rejected(limit):
    with pytest.raises(ValueError):
        RateLimiter(limit, 60)
    with pytest.raises(ValueError):
        cached_call(rate_limit=limit)


@pytest.mark.parametrize("period", [0, -10])
def test_invalid_rate_period_rejected(period):
    with pytest.raises(ValueError):
        RateLimiter(5, period)
    with pytest.raises(ValueError):
        cached_call(rate_limit=5, rate_period=period)


def test_cache_hit_bypasses_rate_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call(rate_limit=1, rate_period=60)
    def generate(prompt):
        nonlocal calls
        calls += 1
        return prompt

    assert generate("hi") == "hi"  # consumes the single slot
    started = time.monotonic()
    assert generate("hi") == "hi"  # cache hit: immediate, no waiting
    assert time.monotonic() - started < 5
    assert calls == 1


def test_acquire_reports_whether_it_had_to_wait():
    fast = RateLimiter(5, 60)
    assert fast.acquire() is False
    assert fast.acquire() is False  # capacity available: no wait

    tight = RateLimiter(1, 0.3)
    assert tight.acquire() is False
    assert tight.acquire() is True  # window full: had to wait


def test_no_rate_limit_configured_means_no_limiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()  # rate_limit unset -> no limiter at all
    def work(n):
        nonlocal calls
        calls += 1
        return n

    started = time.monotonic()
    assert [work(n) for n in range(20)] == list(range(20))
    assert time.monotonic() - started < 5
    assert calls == 20  # distinct arguments: 20 executions, none delayed
