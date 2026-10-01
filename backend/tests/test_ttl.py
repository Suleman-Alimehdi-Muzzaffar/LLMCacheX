"""Tests for TTL expiration in cached_call and SQLiteStorage."""

import time

import pytest

from llmcachex import cached_call


def test_no_ttl_never_expires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def generate(prompt):
        nonlocal calls
        calls += 1
        return prompt

    assert generate("hi") == "hi"
    assert generate("hi") == "hi"
    assert calls == 1


def test_short_ttl_initially_available_then_expires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call(ttl_seconds=0.1)
    def generate(prompt):
        nonlocal calls
        calls += 1
        return f"{prompt}-{calls}"

    first = generate("hi")
    assert generate("hi") == first
    assert calls == 1
    time.sleep(0.25)
    # Expired value must never be returned; function runs again.
    second = generate("hi")
    assert second != first
    assert calls == 2
    # Fresh value is cached again.
    assert generate("hi") == second
    assert calls == 2


def test_ttl_hours_accepted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call(ttl_hours=24)
    def generate(prompt):
        nonlocal calls
        calls += 1
        return prompt

    assert generate("hi") == "hi"
    assert generate("hi") == "hi"
    assert calls == 1


def test_both_ttl_options_rejected():
    with pytest.raises(ValueError, match="only one"):
        cached_call(ttl_hours=1, ttl_seconds=60)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ttl_hours": 0},
        {"ttl_hours": -1},
        {"ttl_seconds": 0},
        {"ttl_seconds": -10},
    ],
)
def test_invalid_ttl_rejected(kwargs):
    with pytest.raises(ValueError):
        cached_call(**kwargs)
