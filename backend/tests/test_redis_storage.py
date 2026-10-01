"""Phase 12 unit tests for the Redis cache backend.

Fully offline: a dict-backed fake stands in for redis-py, so the normal
suite never needs a server. Live-server coverage lives in
``test_redis_integration.py`` (``pytest -m redis``).
"""

from __future__ import annotations

import fnmatch
import json
import sys
import time
import types
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from llmcachex import cached_call
from llmcachex.decorator import _build_request_data
from llmcachex.hashing import hash_request
from llmcachex.storage import (
    DEFAULT_KEY_PREFIX,
    CacheStorage,
    RedisStorage,
    RedisStorageError,
    SQLiteStorage,
    create_storage,
    redact_redis_url,
)


# ------------------------------------------------------------------ fakes ---


class FakeRedisError(Exception):
    """Stand-in for redis-py command errors."""


class FakeRedis:
    """Minimal dict-backed fake of the redis-py surface we use."""

    def __init__(self):
        self.data: dict[str, tuple[str, float | None]] = {}
        self.set_kwargs: list[dict] = []
        self.closed = False

    # -- connection --
    def ping(self):
        return True

    # -- data --
    def _live(self, key):
        item = self.data.get(key)
        if item is None:
            return None
        value, deadline = item
        if deadline is not None and time.monotonic() >= deadline:
            del self.data[key]
            return None
        return value

    def get(self, key):
        return self._live(key)

    def set(self, key, value, ex=None, **kwargs):
        self.set_kwargs.append({"key": key, "ex": ex})
        deadline = None if ex is None else time.monotonic() + ex
        self.data[key] = (value, deadline)
        return True

    def exists(self, key):
        return 1 if self._live(key) is not None else 0

    def scan_iter(self, match=None, count=None):
        for key in list(self.data):
            if self._live(key) is None:
                continue
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.data:
                del self.data[key]
                removed += 1
        return removed

    def close(self):
        self.closed = True


def make_storage(**kwargs):
    """RedisStorage wired to a fake client (no server, no redis-py)."""
    fake = kwargs.pop("client", FakeRedis())
    return RedisStorage(client=fake, **kwargs), fake


def fake_redis_module(monkeypatch, fake):
    """Install a fake ``redis`` module so ``Redis.from_url`` is captured."""
    seen: list[dict] = []
    module = types.ModuleType("redis")

    class FakeFactory:
        @staticmethod
        def from_url(url, **kwargs):
            seen.append({"url": url, **kwargs})
            return fake

    module.Redis = FakeFactory
    monkeypatch.setitem(sys.modules, "redis", module)
    return seen


# --------------------------------------------------------------- protocol ---


def test_redis_storage_satisfies_cache_protocol():
    assert isinstance(RedisStorage.__new__(RedisStorage), CacheStorage)
    assert isinstance(SQLiteStorage.__new__(SQLiteStorage), CacheStorage)


def test_factory_strategies():
    assert isinstance(create_storage("sqlite", cache_path=":memory:"), SQLiteStorage)
    fake = FakeRedis()
    storage = create_storage(
        "redis", redis_url="redis://localhost:6379/0", redis_client=fake
    )
    assert isinstance(storage, RedisStorage)
    with pytest.raises(ValueError, match="Unsupported cache strategy"):
        create_storage("memcached")


# ----------------------------------------------------------- init/errors ---


def test_init_validates_url_and_prefix():
    fake = FakeRedis()
    with pytest.raises(ValueError, match="redis_url"):
        RedisStorage(redis_url="", client=fake)
    with pytest.raises(ValueError, match="redis://"):
        RedisStorage(redis_url="http://localhost:6379", client=fake)
    with pytest.raises(ValueError, match="key_prefix"):
        RedisStorage(key_prefix="  ", client=fake)


def test_prefix_normalized_with_colon():
    storage, _ = make_storage(key_prefix="custom")
    assert storage.key_prefix == "custom:"
    assert storage._key("abc") == "custom:abc"


def test_missing_redis_package_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(RedisStorageError, match=r'llmcachex\[redis\]'):
        RedisStorage(redis_url="redis://localhost:6379/0")


def test_unreachable_server_raises_actionable_error():
    class DeadClient(FakeRedis):
        def ping(self):
            raise FakeRedisError("refused")

    with pytest.raises(RedisStorageError, match="unavailable") as exc_info:
        RedisStorage(
            redis_url="redis://localhost:6399/0", client=DeadClient()
        )
    assert "6399" in str(exc_info.value)


def test_command_failures_wrapped_without_credentials():
    class Flaky(FakeRedis):
        def get(self, key):
            raise FakeRedisError("boom")

    storage, _ = make_storage(
        redis_url="redis://user:secret@localhost:6379/0", client=Flaky()
    )
    with pytest.raises(RedisStorageError, match="Redis read failed") as exc_info:
        storage.get("h")
    assert "secret" not in str(exc_info.value)
    assert "user:***@" in str(exc_info.value)


def test_closed_storage_raises():
    storage, fake = make_storage()
    storage.close()
    storage.close()  # repeated calls are safe
    # Injected clients are not owned, so never closed out from under us.
    assert fake.closed is False
    with pytest.raises(RedisStorageError, match="closed"):
        storage.get("h")


def test_owned_client_closed_on_close(monkeypatch):
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)
    storage = RedisStorage(redis_url="redis://localhost:6379/0")
    storage.close()
    assert fake.closed is True


# --------------------------------------------------------------- redaction ---


def test_redact_redis_url():
    assert (
        redact_redis_url("redis://user:secret@localhost:6379/0")
        == "redis://user:***@localhost:6379/0"
    )
    assert (
        redact_redis_url("redis://:secret@localhost:6379")
        == "redis://***@localhost:6379"
    )
    assert (
        redact_redis_url("redis://localhost:6379/0")
        == "redis://localhost:6379/0"
    )
    assert "secret" not in repr(
        RedisStorage(
            redis_url="redis://user:secret@localhost:6379/0",
            client=FakeRedis(),
        )
    )


# ------------------------------------------------------------ operations ---


def test_set_get_roundtrip_and_key_namespace():
    storage, fake = make_storage()
    storage.set("h1", {"answer": 42})
    assert storage.get("h1") == {"answer": 42}
    assert storage.get_entry("h1").response == {"answer": 42}
    assert list(fake.data) == [f"{DEFAULT_KEY_PREFIX}h1"]
    document = json.loads(fake.data[f"{DEFAULT_KEY_PREFIX}h1"][0])
    assert document["request_hash"] == "h1"
    assert document["expires_at"] is None
    assert "created_at" in document
    # A bare hash is never used as a key.
    assert "h1" not in fake.data


def test_set_requires_json_serializable():
    storage, _ = make_storage()

    class NotJson:
        pass

    with pytest.raises(ValueError, match="not JSON"):
        storage.set("h", NotJson())
    with pytest.raises(TypeError, match="expires_at"):
        storage.set("h", 1, expires_at="tomorrow")


def test_missing_key_is_a_miss():
    storage, _ = make_storage()
    assert storage.get("nope") is None
    assert storage.get_entry("nope") is None
    assert storage.exists("nope") is False


def test_expired_entry_is_a_miss_but_kept():
    storage, fake = make_storage()
    payload = json.dumps(
        {
            "response": {"a": 1},
            "created_at": "2000-01-01T00:00:00+00:00",
            "expires_at": "2000-01-01T00:00:00+00:00",
            "request_hash": "old",
        }
    )
    fake.data[f"{DEFAULT_KEY_PREFIX}old"] = (payload, None)
    assert storage.get("old") is None
    assert storage.exists("old") is False
    # Expired rows are left in place (SQLite parity), not auto-deleted.
    assert f"{DEFAULT_KEY_PREFIX}old" in fake.data


def test_ttl_configures_native_expiration():
    storage, fake = make_storage()
    future = datetime(2999, 1, 1, tzinfo=timezone.utc)
    storage.set("h", {"a": 1}, expires_at=future)
    assert fake.set_kwargs[-1]["ex"] is not None
    assert fake.set_kwargs[-1]["ex"] > 0
    assert storage.get("h") == {"a": 1}


def test_no_ttl_means_persistent_key():
    storage, fake = make_storage()
    storage.set("h", [1, 2, 3])
    assert fake.set_kwargs[-1]["ex"] is None


def test_corrupt_value_raises_and_is_not_deleted():
    storage, fake = make_storage()
    fake.data[f"{DEFAULT_KEY_PREFIX}bad"] = ("{not-json", None)
    with pytest.raises(RedisStorageError, match="Corrupted cache value"):
        storage.get("bad")
    with pytest.raises(RedisStorageError, match="Corrupted cache value"):
        storage.get_entry("bad")
    assert f"{DEFAULT_KEY_PREFIX}bad" in fake.data
    fake.data[f"{DEFAULT_KEY_PREFIX}odd"] = ('[1, 2, 3]', None)
    with pytest.raises(RedisStorageError, match="unexpected shape"):
        storage.get("odd")


def test_exists_and_list_entries():
    storage, _ = make_storage()
    storage.set("a", 1)
    storage.set("b", 2)
    assert storage.exists("a") is True
    metas = storage.list_entries()
    assert {m.request_hash for m in metas} == {"a", "b"}
    assert all(m.expired is False for m in metas)
    assert all(m.created_at for m in metas)


def test_clear_only_removes_namespace():
    storage, fake = make_storage()
    storage.set("a", 1)
    storage.set("b", 2)
    fake.data["other-app:key"] = ('{"x": 1}', None)
    fake.data["llmcachex:other:1"] = ('{"x": 1}', None)
    removed = storage.clear()
    assert removed == 2
    assert "other-app:key" in fake.data
    assert "llmcachex:other:1" in fake.data
    assert storage.list_entries() == []


# -------------------------------------------------------------- decorator ---


def test_decorator_redis_miss_then_hit(tmp_path, monkeypatch):
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)
    calls = []

    @cached_call(
        strategy="redis",
        redis_url="redis://localhost:6379/0",
        cache_path=tmp_path / "analytics.db",
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    assert generate("hi") == {"answer": "hi"}
    assert generate("hi") == {"answer": "hi"}
    assert calls == ["hi"]  # executed exactly once
    keys = [k for k in fake.data if k.startswith(DEFAULT_KEY_PREFIX)]
    assert len(keys) == 1


def test_decorator_redis_custom_prefix_and_expiry(tmp_path, monkeypatch):
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)

    @cached_call(
        strategy="redis",
        redis_url="redis://localhost:6379/0",
        redis_prefix="testns",
        cache_path=tmp_path / "analytics.db",
    )
    def generate(prompt):
        return {"answer": prompt}

    assert generate("hi") == {"answer": "hi"}
    keys = list(fake.data)
    assert len(keys) == 1
    assert keys[0].startswith("testns:")
    # The default namespace is untouched by the custom prefix.
    assert not any(k.startswith(DEFAULT_KEY_PREFIX) for k in keys)


def test_decorator_redis_exceptions_not_cached(tmp_path, monkeypatch):
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)
    attempts = []

    @cached_call(
        strategy="redis",
        cache_path=tmp_path / "analytics.db",
    )
    def flaky(x):
        attempts.append(x)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"x": x}

    with pytest.raises(RuntimeError, match="boom"):
        flaky(1)
    assert flaky(1) == {"x": 1}
    assert attempts == [1, 1]


def test_decorator_redis_uses_same_hash_as_sqlite(tmp_path, monkeypatch):
    """The logical request hash is backend-independent (same inputs)."""
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)

    @cached_call(
        strategy="redis",
        cache_path=tmp_path / "analytics.db",
    )
    def generate(prompt):
        return prompt

    generate("same-input")
    expected = hash_request(
        _build_request_data(generate.__wrapped__, {"prompt": "same-input"})
    )
    assert list(fake.data) == [f"{DEFAULT_KEY_PREFIX}{expected}"]


# --------------------------------------------------------------- security ---


def test_no_unsafe_redis_commands_or_pickle_in_source():
    """Forbid dangerous *usage*; docstrings may name what is avoided."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "llmcachex"
    forbidden = [
        re.compile(r"\.flushdb\s*\("),
        re.compile(r"\.flushall\s*\("),
        re.compile(r"\bFLUSHDB\s*\("),
        re.compile(r"\bFLUSHALL\s*\("),
        re.compile(r"\bimport pickle\b"),
        re.compile(r"\bpickle\."),
        re.compile(r"\.keys\s*\("),
        re.compile(r'''["']KEYS'''),
        re.compile(r"\bKEYS\s+\*"),
    ]
    violations = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                violations.append(f"{path.name}:{line}: {pattern.pattern}")
    assert violations == []


# ------------------------------------------------------------------- api ---


def _redis_app(tmp_path, monkeypatch, fake=None, **kwargs):
    """TestClient serving a fake-backed Redis strategy app."""
    fake = fake if fake is not None else FakeRedis()
    fake_redis_module(monkeypatch, fake)
    params = {
        "cache_db": str(tmp_path / "api.db"),
        "cache_strategy": "redis",
        "redis_url": "redis://localhost:6379/0",
    }
    params.update(kwargs)
    app = create_app(**params)
    return app, fake


def test_api_health_reports_redis_backend(tmp_path, monkeypatch):
    app, _ = _redis_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["cache_backend"] == "redis"
    app.state.service.close()


def test_api_cache_roundtrip_over_redis(tmp_path, monkeypatch):
    fake = FakeRedis()
    fake_redis_module(monkeypatch, fake)
    app, _ = _redis_app(tmp_path, monkeypatch, fake=fake)

    @cached_call(
        strategy="redis",
        redis_url="redis://localhost:6379/0",
        cache_path=tmp_path / "api.db",
    )
    def generate_api(prompt):
        return {"answer": prompt}

    assert generate_api("shared") == {"answer": "shared"}  # miss
    assert generate_api("shared") == {"answer": "shared"}  # hit
    with TestClient(app) as client:
        body = client.get("/api/cache").json()
        assert body["count"] == 1
        assert body["entries"][0]["expired"] is False
        assert "answer" not in client.get("/api/cache").text
        cleared = client.delete("/api/cache").json()
        assert cleared["cleared"] is True
        assert client.get("/api/cache").json()["count"] == 0
    app.state.service.close()


def test_api_redis_failure_is_clean_503_without_credentials(
    tmp_path, monkeypatch
):
    class Flaky(FakeRedis):
        def scan_iter(self, match=None, count=None):
            raise FakeRedisError("down")

    secret_url = "redis://user:s3cret-pw@localhost:6379/0"
    app, _ = _redis_app(
        tmp_path, monkeypatch, fake=Flaky(), redis_url=secret_url
    )
    with TestClient(app) as client:
        response = client.get("/api/cache")
        assert response.status_code == 503
        assert response.json() == {"detail": "Cache backend error"}
        assert "s3cret-pw" not in response.text
        assert "Traceback" not in response.text
    app.state.service.close()


def test_api_strategy_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_STRATEGY", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    fake_redis_module(monkeypatch, FakeRedis())
    app = create_app(cache_db=str(tmp_path / "env.db"))
    with TestClient(app) as client:
        assert client.get("/api/health").json()["cache_backend"] == "redis"
    app.state.service.close()


def test_api_unknown_strategy_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_STRATEGY", "memcached")
    app = create_app(cache_db=str(tmp_path / "env.db"))
    with pytest.raises(ValueError, match="Unsupported cache strategy"):
        with TestClient(app):
            pass  # startup must fail, never serve
