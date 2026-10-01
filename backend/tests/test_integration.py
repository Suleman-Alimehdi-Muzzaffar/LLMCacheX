"""Combined Phase 4/5 integration tests: TTL + retry + rate limiting."""

import time

from llmcachex import cached_call


def test_combined_ttl_retry_rate_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executions = 0

    @cached_call(
        ttl_seconds=0.2,
        retries=2,
        backoff_factor=0,
        rate_limit=10,
        rate_period=60,
    )
    def generate(prompt):
        nonlocal executions
        executions += 1
        if executions == 1:
            raise ConnectionError("transient failure")
        return f"{prompt}-{executions}"

    # First logical call: fails once, retry succeeds, result cached.
    assert generate("hi") == "hi-2"
    assert executions == 2

    # Cache hit: no execution, no rate-limit consumption.
    assert generate("hi") == "hi-2"
    assert executions == 2

    # After expiry: executes again and caches the fresh value.
    time.sleep(0.35)
    assert generate("hi") == "hi-3"
    assert executions == 3
    assert generate("hi") == "hi-3"
    assert executions == 3


def test_retry_plus_rate_limiter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executions = 0

    @cached_call(retries=2, backoff_factor=0, rate_limit=5, rate_period=60)
    def flaky(prompt):
        nonlocal executions
        executions += 1
        if executions == 1:
            raise ConnectionError("transient")
        return f"{prompt}:{executions}"

    # Retry runs after rate limiting; only successful result is cached.
    assert flaky("x") == "x:2"
    assert executions == 2
    assert flaky("x") == "x:2"
    assert executions == 2


def test_ttl_plus_rate_limiter_expired_entry_passes_through(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executions = 0

    @cached_call(ttl_seconds=0.1, rate_limit=1, rate_period=0.5)
    def generate(prompt):
        nonlocal executions
        executions += 1
        return f"{prompt}-{executions}"

    # First miss consumes the single rate-limit slot.
    assert generate("hi") == "hi-1"
    assert generate("hi") == "hi-1"  # hit: no slot consumed
    assert executions == 1

    # Expired entry counts as a real execution and must pass through the
    # rate limiter, so the call waits for window capacity.
    time.sleep(0.25)
    started = time.monotonic()
    assert generate("hi") == "hi-2"
    elapsed = time.monotonic() - started
    assert executions == 2
    assert elapsed >= 0.2, elapsed  # waited for capacity; not instant


def test_ttl_plus_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executions = 0

    @cached_call(ttl_seconds=0.15, retries=2, backoff_factor=0)
    def flaky():
        nonlocal executions
        executions += 1
        if executions == 1:
            raise ConnectionError("transient")
        return f"v{executions}"

    # Retry succeeds, result is cached with the TTL.
    assert flaky() == "v2"
    assert executions == 2
    assert flaky() == "v2"
    assert executions == 2
    # After expiry the retry cycle starts fresh.
    time.sleep(0.3)
    assert flaky() == "v3"
    assert executions == 3
