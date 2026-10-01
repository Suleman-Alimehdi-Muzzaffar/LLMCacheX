"""Phase 5 edge-case tests: hashing, JSON, corruption, paths, API surface."""

import json
import logging
import sqlite3

import pytest

import llmcachex
from llmcachex import cached_call
from llmcachex.hashing import hash_request
from llmcachex.storage import SQLiteStorage


# ---------------------------------------------------------------- hashing ---


def test_same_input_twice_produces_same_hash():
    data = {"prompt": "Hello", "model": "test", "config": {"t": 0.5}}
    assert hash_request(data) == hash_request(dict(data))


def test_nested_lists_are_deterministic():
    first = {"items": [[1, 2], [3, {"a": 1}]]}
    second = {"items": [[1, 2], [3, {"a": 1}]]}
    assert hash_request(first) == hash_request(second)


def test_nested_list_order_matters():
    assert hash_request({"items": [1, 2]}) != hash_request({"items": [2, 1]})


def test_different_nested_structures_do_not_collide():
    assert hash_request({"a": {"b": 1}}) != hash_request({"a": [1]})
    assert hash_request({"a": [1, 2]}) != hash_request({"a": [[1, 2]]})


def test_supported_primitives_hash_differently():
    digests = {hash_request(v) for v in (None, True, False, 0, 1, 1.0, "", "x")}
    # None/True/False/0/1/1.0/""/"x" must all be distinct identities.
    assert len(digests) == 8


def test_function_identity_data_is_deterministic():
    identity = {
        "function": {"module": "example", "qualname": "outer.inner"},
        "arguments": {"prompt": "Hi", "options": {"temperature": 0.7}},
    }
    reordered = {
        "arguments": {"options": {"temperature": 0.7}, "prompt": "Hi"},
        "function": {"qualname": "outer.inner", "module": "example"},
    }
    assert hash_request(identity) == hash_request(reordered)
    other = {
        "function": {"module": "example", "qualname": "outer.other"},
        "arguments": {"prompt": "Hi", "options": {"temperature": 0.7}},
    }
    assert hash_request(identity) != hash_request(other)


# ------------------------------------------------------------- JSON types ---


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        42,
        -7,
        3.14,
        "text",
        "",
        [1, 2, 3],
        {"a": 1},
        {"nested": {"list": [{"deep": None}]}},
        [None, True, "x", 1.5, {"k": [1]}],
    ],
)
def test_json_compatible_values_round_trip(tmp_path, value):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        storage.set("h", value)
        assert storage.get("h") == value
        assert type(storage.get("h")) is type(value)  # 1 stays int, True bool
    finally:
        storage.close()


def test_non_json_return_values_fail_clearly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def returns_set():
        return {1, 2, 3}

    with pytest.raises(TypeError, match="not JSON serializable"):
        returns_set()

    @cached_call()
    def returns_file():
        return open(__file__, "r", encoding="utf-8")

    with pytest.raises(TypeError, match="not JSON serializable"):
        returns_file()


def test_cached_none_value_is_a_valid_cache_hit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call()
    def returns_none():
        nonlocal calls
        calls += 1
        return None

    assert returns_none() is None
    assert returns_none() is None
    assert calls == 1  # second call came from cache, not re-execution


# -------------------------------------------------------------- corruption ---


def test_corrupt_json_raises_clear_error_and_is_not_deleted(tmp_path):
    db = tmp_path / "cache.db"
    storage = SQLiteStorage(db)
    storage.set("h", {"ok": 1})
    storage.close()
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE cache_entries SET response_json = ? WHERE request_hash = ?",
            ("{not-json", "h"),
        )
        con.commit()

    reopened = SQLiteStorage(db)
    try:
        # Corruption must surface as an error, not a silent cache miss.
        with pytest.raises(json.JSONDecodeError):
            reopened.get("h")
        with pytest.raises(json.JSONDecodeError):
            reopened.get_entry("h")
        with pytest.raises(json.JSONDecodeError):
            reopened.exists("h")
        # Corrupted data must not be auto-deleted.
        with sqlite3.connect(db) as con:
            count = con.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]
        assert count == 1
    finally:
        reopened.close()


# ------------------------------------------------- best-effort analytics ---


def test_broken_analytics_writes_never_break_a_cached_call(tmp_path, caplog):
    """Part 11: analytics failure must not destroy a successful call.

    With the analytics table dropped (simulating a broken/locked
    analytics store), cache hits and misses must still return the right
    value, still cache, and log a warning instead of raising.
    """
    db = tmp_path / "cache.db"
    calls = 0

    @cached_call(cache_path=db)
    def compute(x):
        nonlocal calls
        calls += 1
        return {"x": x}

    assert compute(1) == {"x": 1}  # miss: creates tables, records fine

    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE analytics_events")
        con.commit()

    with caplog.at_level(logging.WARNING, logger="llmcachex.decorator"):
        assert compute(1) == {"x": 1}  # hit despite broken analytics
        assert compute(2) == {"x": 2}  # miss: execute + store + record
        assert compute(2) == {"x": 2}  # hit again

    assert calls == 2  # one execution per distinct argument only
    assert any(
        "continuing without it" in record.getMessage()
        for record in caplog.records
    )


def test_original_exception_survives_broken_analytics_recording(tmp_path):
    """A failing call must propagate its own error, not a sqlite error."""
    db = tmp_path / "cache.db"

    @cached_call(cache_path=db)
    def failing():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        failing()  # first call: analytics tables still exist

    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE analytics_events")
        con.commit()

    # The failure-path event write also fails — the user's RuntimeError
    # must still be what propagates (previously masked by sqlite errors).
    with pytest.raises(RuntimeError, match="boom"):
        failing()


# ------------------------------------------------------------ path handling ---


def test_absolute_path_storage(tmp_path):
    db = tmp_path / "abs" / "cache.db"
    storage = SQLiteStorage(db)
    try:
        storage.set("h", 1)
        assert db.exists()
    finally:
        storage.close()


def test_nested_parent_directories_are_created(tmp_path):
    db = tmp_path / "a" / "b" / "c" / "cache.db"
    storage = SQLiteStorage(db)  # regression: used to raise OperationalError
    try:
        storage.set("h", "nested")
        assert storage.get("h") == "nested"
    finally:
        storage.close()


def test_relative_path_with_nested_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    storage = SQLiteStorage("sub/dir/cache.db")
    try:
        storage.set("h", 1)
        assert (tmp_path / "sub" / "dir" / "cache.db").exists()
    finally:
        storage.close()


def test_decorator_accepts_nested_cache_path(tmp_path):
    db = tmp_path / "deep" / "nested" / "decorator.db"

    @cached_call(cache_path=db)
    def work():
        return "ok"

    assert work() == "ok"
    assert db.exists()


# --------------------------------------------------------------- API surface ---


def test_public_imports():
    assert callable(llmcachex.cached_call)
    assert llmcachex.__version__ == "0.1.0"
    from llmcachex import cached_call as imported

    assert imported is llmcachex.cached_call
    assert set(llmcachex.__all__) == {"cached_call", "__version__"}


def test_explicit_sqlite_strategy_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = 0

    @cached_call(strategy="sqlite")
    def generate(x):
        nonlocal calls
        calls += 1
        return x

    assert generate(1) == generate(1)
    assert calls == 1


def test_missing_argument_raises_clear_type_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def add(a, b):
        return a + b

    with pytest.raises(TypeError, match="missing a required argument"):
        add(1)


def test_non_string_dict_keys_in_arguments_raise_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    @cached_call()
    def lookup(d):
        return d

    # Integer keys cannot be hashed deterministically (JSON object keys
    # must be strings): must fail loudly, never silently coerce.
    with pytest.raises(TypeError, match="string keys"):
        lookup({1: "x"})


def test_closed_storage_raises_clear_error(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    storage.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        storage.get("h")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        storage.set("h", 1)


def test_sql_is_parameterized_hash_round_trips_literal(tmp_path):
    storage = SQLiteStorage(tmp_path / "cache.db")
    try:
        # If SQL were built by concatenation, this would alter/delete data.
        hostile = "x'); DROP TABLE cache_entries; --"
        storage.set(hostile, {"safe": True})
        assert storage.get(hostile) == {"safe": True}
        with sqlite3.connect(tmp_path / "cache.db") as con:
            tables = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert ("cache_entries",) in tables
    finally:
        storage.close()


def test_decorator_reads_cache_exactly_once_per_call(tmp_path, monkeypatch):
    """Regression: the wrapper used exists()+get() (two queries); an entry
    expiring between them would hand None to the caller. It now performs a
    single get_entry() read."""
    monkeypatch.chdir(tmp_path)
    import llmcachex.decorator as decorator_module

    real_get_entry = SQLiteStorage.get_entry
    calls = {"count": 0}

    def counting_get_entry(self, request_hash):
        calls["count"] += 1
        return real_get_entry(self, request_hash)

    monkeypatch.setattr(SQLiteStorage, "get_entry", counting_get_entry)

    @decorator_module.cached_call()
    def work(x):
        return x

    assert work(1) == 1
    assert calls["count"] == 1  # miss: exactly one read
    assert work(1) == 1
    assert calls["count"] == 2  # hit: exactly one more read
