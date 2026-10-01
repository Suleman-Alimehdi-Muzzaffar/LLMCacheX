"""Tests for the Phase 3 decorator behavior (still valid in Phase 4)."""

import pytest

from llmcachex import cached_call


def test_cache_hit_avoids_reexecution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def generate(prompt, model):
        nonlocal calls
        calls += 1
        return {"prompt": prompt, "model": model}

    first = generate("Hello", "test-model")
    second = generate("Hello", "test-model")
    assert first == second == {"prompt": "Hello", "model": "test-model"}
    assert calls == 1


def test_cache_miss_on_different_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def generate(prompt):
        nonlocal calls
        calls += 1
        return prompt

    generate("a")
    generate("b")
    assert calls == 2


def test_positional_and_keyword_calls_share_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def add(a, b):
        nonlocal calls
        calls += 1
        return a + b

    assert add(1, 2) == 3
    assert add(a=1, b=2) == 3
    assert add(b=2, a=1) == 3
    assert calls == 1


def test_function_identity_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def function_a(value):
        return "A"

    @cached_call()
    def function_b(value):
        return "B"

    assert function_a("test") == "A"
    assert function_b("test") == "B"


def test_exceptions_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = 0

    @cached_call()
    def failing():
        nonlocal attempts
        attempts += 1
        raise ValueError("failure")

    with pytest.raises(ValueError, match="failure"):
        failing()
    with pytest.raises(ValueError, match="failure"):
        failing()
    assert attempts == 2


def test_metadata_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def my_function():
        """Example documentation."""
        return "hello"

    assert my_function.__name__ == "my_function"
    assert my_function.__doc__ == "Example documentation."


def test_custom_cache_path(tmp_path):
    db = tmp_path / "phase3_custom.db"

    @cached_call(cache_path=db)
    def custom():
        return "hello"

    assert custom() == "hello"
    assert db.exists()


def test_non_json_return_raises_type_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def returns_object():
        return object()

    with pytest.raises(TypeError, match="not JSON serializable"):
        returns_object()


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError, match="Unsupported cache strategy"):
        cached_call(strategy="memcached")


def test_redis_strategy_accepted_at_decoration_time():
    # Redis availability is checked on first call, not at decoration.
    decorator = cached_call(strategy="redis")
    assert callable(decorator)


def test_default_arguments_are_normalized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def func(a, b=10):
        nonlocal calls
        calls += 1
        return a + b

    # func(5), func(5, 10) and func(5, b=10) are the same logical request
    # because apply_defaults() binds the default explicitly.
    assert func(5) == 15
    assert func(5, 10) == 15
    assert func(5, b=10) == 15
    assert calls == 1


def test_mixed_positional_and_keyword_calls_share_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def func(a, b, c=3):
        nonlocal calls
        calls += 1
        return a + b + c

    assert func(1, 2) == 6
    assert func(1, b=2) == 6
    assert func(1, 2, c=3) == 6
    assert func(a=1, b=2, c=3) == 6
    assert calls == 1


def test_cache_hit_returns_same_logical_response(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def generate():
        return {"items": [1, 2, 3], "ok": True}

    first = generate()
    second = generate()
    assert first == second == {"items": [1, 2, 3], "ok": True}
    # Cached copies are independent JSON parses, not aliased objects.
    assert first is not second
    first["items"].append(4)
    assert generate() == {"items": [1, 2, 3], "ok": True}
