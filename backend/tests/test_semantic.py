"""Phase 13 tests for opt-in semantic caching.

Fully deterministic: a scripted fake embedding provider stands in for
the real local model, so the standard suite needs no downloads, no
network and no ML stack. The single live-model test is marked
``semantic`` (``pytest -m semantic``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import sqlite3
import sys
import time
import types

import pytest

from llmcachex import cached_call
from llmcachex.analytics import (
    AnalyticsStore,
    HitType,
    RequestEvent,
    Usage,
)
from llmcachex.metrics import MetricsRecorder
from llmcachex.semantic import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    SemanticError,
    SemanticRecord,
    SemanticStore,
    cosine_similarity,
    resolve_model_name,
)


# ------------------------------------------------------------------ fakes ---


def _fallback_vector(text: str, dim: int = 8) -> list[float]:
    """Deterministic pseudo-random unit vector (unstubbed texts)."""
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    state = seed % (2**31 - 1) or 1
    values = []
    for _ in range(dim):
        state = (1103515245 * state + 12345) % (2**31)
        values.append((state / 2**31) * 2.0 - 1.0)
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class FakeEmbedder(EmbeddingProvider):
    """Scripted provider; unknown texts get stable pseudo-vectors."""

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        model: str = "fake-model-1",
        dim: int = 8,
    ):
        self.vectors = dict(vectors or {})
        self._model = model
        self._dim = dim
        self.embed_calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        if text in self.vectors:
            return list(self.vectors[text])
        return _fallback_vector(text, self._dim)

    def dimension(self) -> int | None:
        return self._dim

    def model_name(self) -> str:
        return self._model


def unit(*components: float) -> list[float]:
    """Normalized vector helper for precise similarity fixtures."""
    norm = math.sqrt(sum(c * c for c in components)) or 1.0
    return [c / norm for c in components]


class FakeRedis:
    """Minimal dict-backed redis-py surface (mirrors redis test fakes)."""

    def __init__(self):
        self.data: dict[str, tuple[str, float | None]] = {}

    def ping(self):
        return True

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
        pass


def fake_redis_module(monkeypatch, fake):
    module = types.ModuleType("redis")

    class FakeFactory:
        @staticmethod
        def from_url(url, **kwargs):
            return fake

    module.Redis = FakeFactory
    monkeypatch.setitem(sys.modules, "redis", module)


# ------------------------------------------------------------- similarity ---


def test_cosine_identical_is_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine_similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one():
    assert cosine_similarity([2.0, 0.0], [-3.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero_not_error():
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0], [0.0]) == 0.0


def test_cosine_rejects_mismatched_and_empty():
    with pytest.raises(ValueError, match="different lengths"):
        cosine_similarity([1.0, 0.0], [1.0])
    with pytest.raises(ValueError, match="empty"):
        cosine_similarity([], [])


def test_cosine_known_value():
    assert cosine_similarity([1.0, 1.0], [1.0, 0.0]) == pytest.approx(
        math.sqrt(2) / 2
    )


# --------------------------------------------------------------- provider ---


def test_local_provider_lazy_and_identified():
    provider = LocalEmbeddingProvider(model="my-model")
    assert provider.model_name() == "my-model"
    assert provider.dimension() is None  # nothing loaded yet


def test_resolve_model_name_defaults_and_env(monkeypatch):
    monkeypatch.delenv("SEMANTIC_MODEL", raising=False)
    from llmcachex.semantic import DEFAULT_SEMANTIC_MODEL

    assert resolve_model_name(None) == DEFAULT_SEMANTIC_MODEL
    assert resolve_model_name("custom") == "custom"
    monkeypatch.setenv("SEMANTIC_MODEL", "env-model")
    assert resolve_model_name(None) == "env-model"
    with pytest.raises(SemanticError, match="non-empty"):
        resolve_model_name("  ")


def test_missing_semantic_dependency_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    provider = LocalEmbeddingProvider(model="my-model")
    with pytest.raises(SemanticError, match=r'llmcachex\[semantic\]'):
        provider.embed("hello")


def _install_fake_fastembed(monkeypatch):
    """Stub the fastembed module so no model is downloaded."""
    module = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name):
            self.model_name = model_name

        def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    module.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return module


def test_load_disables_hub_symlink_warning(monkeypatch):
    from llmcachex.semantic import embeddings as embeddings_module

    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)
    _install_fake_fastembed(monkeypatch)
    embeddings_module._model_cache.pop("stub-model", None)
    provider = LocalEmbeddingProvider(model="stub-model")
    assert provider.embed("hello") == [0.1, 0.2, 0.3]
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"
    embeddings_module._model_cache.pop("stub-model", None)


def test_explicit_symlink_warning_setting_is_respected(monkeypatch):
    from llmcachex.semantic import embeddings as embeddings_module

    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS_WARNING", "0")
    _install_fake_fastembed(monkeypatch)
    embeddings_module._model_cache.pop("stub-model", None)
    provider = LocalEmbeddingProvider(model="stub-model")
    assert provider.embed("hello") == [0.1, 0.2, 0.3]
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "0"
    embeddings_module._model_cache.pop("stub-model", None)


def test_embed_rejects_empty_text():
    provider = LocalEmbeddingProvider.__new__(LocalEmbeddingProvider)
    provider._model_name = "x"
    provider._dimension = None
    with pytest.raises(SemanticError, match="empty string"):
        provider.embed("  ")


def test_fake_embedder_satisfies_protocol():
    assert isinstance(FakeEmbedder(), EmbeddingProvider)


# ------------------------------------------------------------ index store ---


def _record(cache_key, vector, **overrides):
    fields = {
        "cache_key": cache_key,
        "embedding": tuple(vector),
        "embedding_model": "fake-model-1",
        "dimension": len(vector),
        "namespace": "sqlite:test.db",
        "function_name": "mod.fn",
        "provider": None,
        "model": None,
        "fields_signature": "prompt",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": None,
    }
    fields.update(overrides)
    return SemanticRecord(**fields)


def _context(**overrides):
    base = {
        "namespace": "sqlite:test.db",
        "function_name": "mod.fn",
        "provider": None,
        "model": None,
        "embedding_model": "fake-model-1",
        "dimension": 3,
        "fields_signature": "prompt",
        "threshold": 0.9,
    }
    base.update(overrides)
    return base


def test_store_add_find_remove_clear(tmp_path):
    store = SemanticStore(tmp_path / "idx.db")
    try:
        assert store.count() == 0
        store.add(_record("k1", unit(1, 0, 0)))
        store.add(_record("k2", unit(0, 1, 0)))
        assert store.count() == 2
        found = store.find(unit(1, 0, 0), **_context())
        assert [(c.cache_key, round(c.similarity, 6)) for c in found] == [
            ("k1", 1.0)
        ]
        # Re-adding replaces (upsert), retention-bounded.
        store.add(_record("k1", unit(0, 0, 1)))
        assert store.count() == 2
        assert [c.cache_key for c in store.find(unit(0, 0, 1), **_context())] == [
            "k1"
        ]
        store.remove("k1")
        assert store.count() == 1
        assert store.clear() == 1
        assert store.count() == 0
    finally:
        store.close()


def test_store_context_filtering(tmp_path):
    store = SemanticStore(tmp_path / "idx.db")
    try:
        store.add(_record("base", unit(1, 0, 0)))
        store.add(_record("other-fn", unit(1, 0, 0), function_name="mod.other"))
        store.add(_record("other-model", unit(1, 0, 0), model="m2"))
        store.add(
            _record("other-emb", unit(1, 0, 0), embedding_model="fake-model-2")
        )
        store.add(_record("other-ns", unit(1, 0, 0), namespace="redis:x"))
        store.add(
            _record("other-fields", unit(1, 0, 0), fields_signature="prompt,t")
        )
        found = store.find(unit(1, 0, 0), **_context())
        assert [c.cache_key for c in found] == ["base"]
        # A compatible provider-labeled row matches a labeled query.
        store.add(_record("prov", unit(1, 0, 0), provider="p", model="m"))
        assert [
            c.cache_key
            for c in store.find(unit(1, 0, 0), **_context(provider="p", model="m"))
        ] == ["prov"]
    finally:
        store.close()


def test_store_rejects_bad_records(tmp_path):
    store = SemanticStore(tmp_path / "idx.db")
    try:
        with pytest.raises(ValueError, match="empty embedding"):
            store.add(_record("k", []))
        with pytest.raises(ValueError, match="does not match"):
            store.add(_record("k", [1.0], dimension=3))
    finally:
        store.close()


def test_semantic_enabled_without_backend_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)

    with pytest.raises(SemanticError, match=r'llmcachex\[semantic\]'):

        @cached_call(semantic=True, cache_path=tmp_path / "s.db")
        def generate(prompt):
            return prompt


# --------------------------------------------------------------- validation ---


def test_threshold_validation():
    for bad in (-0.1, 1.5, float("nan"), "high", True):
        with pytest.raises(ValueError, match="semantic_threshold"):
            cached_call(semantic_threshold=bad)
    for ok in (0, 1, 0.92, 0.0, 1.0, None):
        assert callable(cached_call(semantic_threshold=ok))


def test_threshold_env_fallback(monkeypatch):
    from llmcachex.decorator import _validate_semantic_threshold

    monkeypatch.delenv("SEMANTIC_THRESHOLD", raising=False)
    assert _validate_semantic_threshold(None) == 0.92
    assert _validate_semantic_threshold(0.5) == 0.5
    monkeypatch.setenv("SEMANTIC_THRESHOLD", "0.81")
    assert _validate_semantic_threshold(None) == 0.81
    # Explicit values always win over the environment.
    assert _validate_semantic_threshold(0.5) == 0.5
    monkeypatch.setenv("SEMANTIC_THRESHOLD", "bogus")
    with pytest.raises(ValueError, match="SEMANTIC_THRESHOLD"):
        _validate_semantic_threshold(None)


def test_fields_validation():
    for bad in ("prompt", [], ["  "], [42], ["ok", ""]):
        with pytest.raises(ValueError, match="semantic_fields"):
            cached_call(semantic_fields=bad)


def test_unknown_semantic_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown: nope"):

        @cached_call(semantic_fields=["prompt", "nope"])
        def generate(prompt):
            return prompt


def test_semantic_flag_must_be_bool():
    with pytest.raises(ValueError, match="semantic must be True or False"):
        cached_call(semantic="yes")


# ------------------------------------------------------- exact regression ---


def test_plain_calls_never_embed(tmp_path):
    embedder = FakeEmbedder()

    @cached_call(cache_path=tmp_path / "plain.db")
    def generate(prompt):
        return {"answer": prompt}

    assert generate("hi") == {"answer": "hi"}
    assert generate("hi") == {"answer": "hi"}
    assert embedder.embed_calls == []
    # No semantic rows exist for exact-only usage.
    store = SemanticStore(tmp_path / "plain.db")
    try:
        assert store.count() == 0
    finally:
        store.close()


def test_hit_type_validation():
    with pytest.raises(ValueError, match="hit_type"):
        RequestEvent(
            event_type="cache_hit",
            function_name="f",
            request_hash="h",
            cache_status="hit",
            hit_type="bogus",
        )
    event = RequestEvent(
        event_type="cache_hit",
        function_name="f",
        request_hash="h",
        cache_status="hit",
        hit_type=HitType.SEMANTIC,
    )
    assert event.hit_type == "semantic"


# ------------------------------------------------------------ semantic hit ---


def test_semantic_hit_returns_prior_response(tmp_path):
    text_a = '{"prompt": "Explain recursion in Python for beginners."}'
    text_b = '{"prompt": "Give a beginner-friendly explanation of recursion."}'
    embedder = FakeEmbedder(
        {text_a: unit(1, 0, 0), text_b: unit(0.99, 0.141, 0)}
    )
    calls = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": f"response-to:{prompt}"}

    first = generate("Explain recursion in Python for beginners.")
    second = generate("Give a beginner-friendly explanation of recursion.")
    assert second == first  # reused, not re-executed
    assert calls == ["Explain recursion in Python for beginners."]
    # Exact hashes genuinely differ (this is not an exact hit).
    from llmcachex.hashing import hash_request

    assert (
        hash_request({"prompt": "Explain recursion in Python for beginners."})
        != hash_request(
            {"prompt": "Give a beginner-friendly explanation of recursion."}
        )
    )


def test_semantic_miss_executes_and_caches(tmp_path):
    embedder = FakeEmbedder(
        {
            '{"prompt": "cats"}': unit(1, 0, 0),
            '{"prompt": "quantum chromodynamics"}': unit(0, 1, 0),
        }
    )
    calls = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    assert generate("cats") == {"answer": "cats"}
    assert generate("quantum chromodynamics") == {
        "answer": "quantum chromodynamics"
    }
    assert calls == ["cats", "quantum chromodynamics"]


def test_threshold_boundary_precise(tmp_path):
    # Similarity exactly 0.9: threshold above misses, below hits.
    vec_a = unit(1, 0, 0)
    vec_b = unit(0.9, math.sqrt(1 - 0.81), 0)
    assert cosine_similarity(vec_a, vec_b) == pytest.approx(0.9)

    def run(threshold):
        db = tmp_path / f"t{threshold}.db"
        embedder = FakeEmbedder(
            {'{"prompt": "A"}': vec_a, '{"prompt": "B"}': vec_b}
        )
        calls = []

        @cached_call(
            cache_path=db,
            semantic=True,
            semantic_threshold=threshold,
            semantic_fields=["prompt"],
            semantic_provider=embedder,
        )
        def generate(prompt):
            calls.append(prompt)
            return {"answer": prompt}

        generate("A")
        generate("B")
        return calls

    assert run(0.95) == ["A", "B"]  # miss: below threshold
    assert run(0.85) == ["A"]  # hit: above threshold


def test_different_functions_stay_isolated(tmp_path):
    text = '{"prompt": "Explain Python"}'
    embedder = FakeEmbedder({text: unit(1, 0, 0)})
    calls = {"a": [], "b": []}

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def function_a(prompt):
        calls["a"].append(prompt)
        return "A"

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def function_b(prompt):
        calls["b"].append(prompt)
        return "B"

    assert function_a("Explain Python") == "A"
    assert function_b("Explain Python") == "B"  # not A's response
    assert calls == {"a": ["Explain Python"], "b": ["Explain Python"]}


def test_different_embedding_models_stay_isolated(tmp_path):
    text_1 = '{"x": 1}'
    text_2 = '{"x": 2}'
    db = tmp_path / "sem.db"
    calls = []

    def make(model, seed_text):
        # Identical vectors, incompatible model identities; distinct
        # arguments keep the exact hashes apart so only the semantic
        # layer is exercised.
        embedder = FakeEmbedder(
            {text_1: unit(1, 0, 0), text_2: unit(1, 0, 0)}, model=model
        )

        @cached_call(
            cache_path=db,
            semantic=True,
            semantic_fields=["x"],
            semantic_provider=embedder,
        )
        def compute(x):
            calls.append((model, x))
            return f"{model}:{x}"

        return compute

    assert make("model-one", text_1)(1) == "model-one:1"
    # Same similarity, incompatible model identity: must re-execute.
    assert make("model-two", text_2)(2) == "model-two:2"
    assert calls == [("model-one", 1), ("model-two", 2)]


def test_provider_model_isolation(tmp_path):
    text_1 = '{"prompt": "hi-one"}'
    text_2 = '{"prompt": "hi-two"}'
    db = tmp_path / "sem.db"
    calls = []

    def make(provider):
        # Same similarity, distinct arguments (no exact-hash collision).
        embedder = FakeEmbedder(
            {text_1: unit(1, 0, 0), text_2: unit(1, 0, 0)}
        )

        @cached_call(
            cache_path=db,
            semantic=True,
            semantic_fields=["prompt"],
            semantic_provider=embedder,
            provider=provider,
        )
        def generate(prompt):
            calls.append((provider, prompt))
            return f"{provider}:{prompt}"

        return generate

    assert make("p1")("hi-one") == "p1:hi-one"
    assert make("p2")("hi-two") == "p2:hi-two"  # different provider: no sharing
    assert calls == [("p1", "hi-one"), ("p2", "hi-two")]


def test_default_fields_include_everything(tmp_path):
    """Without semantic_fields, all bound args shape the embedded text."""
    embedder = FakeEmbedder()
    calls = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_provider=embedder,
    )
    def generate(prompt, temperature=0.7):
        calls.append((prompt, temperature))
        return {"answer": prompt}

    generate("Explain recursion", temperature=0.1)
    embedded = embedder.embed_calls[0]
    assert "Explain recursion" in embedded
    assert "temperature" in embedded  # non-semantic config not ignored
    assert "0.1" in embedded


def test_explicit_fields_scope_matching(tmp_path):
    """Same prompt + different temperature merges only when chosen."""
    prompt_a = '{"prompt": "Explain recursion"}'
    embedder = FakeEmbedder({prompt_a: unit(1, 0, 0)})
    calls = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt, temperature=0.7):
        calls.append(temperature)
        return {"answer": prompt, "t": temperature}

    first = generate("Explain recursion", temperature=0.1)
    second = generate("Explain recursion", temperature=1.0)
    assert second == first  # explicitly scoped to prompt only
    assert calls == [0.1]


def test_exceptions_never_indexed(tmp_path):
    embedder = FakeEmbedder()
    attempts = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_provider=embedder,
    )
    def flaky(prompt):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"answer": prompt}

    with pytest.raises(RuntimeError, match="boom"):
        flaky("hi")
    assert flaky("hi") == {"answer": "hi"}
    assert attempts == ["hi", "hi"]
    store = SemanticStore(tmp_path / "sem.db")
    try:
        # Only the successful miss was indexed.
        assert store.count() == 1
    finally:
        store.close()


# ------------------------------------------------------------------- TTL ---


def test_semantic_hit_before_expiry_miss_after(tmp_path):
    db = tmp_path / "sem.db"
    text_a = '{"prompt": "alpha"}'
    text_b = '{"prompt": "alpha, said differently"}'
    embedder = FakeEmbedder({text_a: unit(1, 0, 0), text_b: unit(1, 0, 0)})
    calls = []

    @cached_call(
        cache_path=db,
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
        ttl_seconds=3600,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    assert generate("alpha") == {"answer": "alpha"}
    assert generate("alpha, said differently") == {"answer": "alpha"}
    assert calls == ["alpha"]  # semantic hit before expiry
    # Expire the underlying exact entry directly (no slow sleeps).
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE cache_entries SET expires_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )
        con.commit()
    assert generate("alpha, said differently") == {
        "answer": "alpha, said differently"
    }
    assert calls == ["alpha", "alpha, said differently"]
    # The stale semantic row was invalidated lazily on the failed match.
    store = SemanticStore(db)
    try:
        assert store.count() == 1
    finally:
        store.close()


# ------------------------------------------------------------------ clear ---


def test_service_clear_removes_semantic_rows(tmp_path):
    from api.services import MetricsService

    db = tmp_path / "sem.db"
    embedder = FakeEmbedder({'{"prompt": "hi"}': unit(1, 0, 0)})

    @cached_call(
        cache_path=db,
        semantic=True,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt):
        return {"answer": prompt}

    generate("hi")
    store = SemanticStore(db)
    try:
        assert store.count() == 1
    finally:
        store.close()
    service = MetricsService(db)
    try:
        assert service.clear_cache().cleared is True
        assert SemanticStore(db).count() == 0
    finally:
        service.close()


# --------------------------------------------------------------- analytics ---


def test_semantic_analytics_counts(tmp_path):
    db = tmp_path / "sem.db"
    texts = {
        '{"prompt": "A"}': unit(1, 0, 0),
        '{"prompt": "A, rephrased"}': unit(1, 0, 0),
        '{"prompt": "zzz unrelated"}': unit(0, 1, 0),
    }
    embedder = FakeEmbedder(texts)
    pricing_calls = []

    def usage(result):
        pricing_calls.append(result)
        return Usage(input_tokens=10, output_tokens=20, total_tokens=30)

    from llmcachex.analytics import PricingConfig

    @cached_call(
        cache_path=db,
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
        usage=usage,
        pricing=PricingConfig.from_values(0.01, 0.03),
    )
    def generate(prompt):
        return {"answer": prompt}

    generate("A")  # miss
    generate("A, rephrased")  # semantic hit
    generate("A")  # exact hit
    assert len(pricing_calls) == 1  # usage extracted only on execution

    metrics = MetricsRecorder(db)
    try:
        snapshot = metrics.snapshot()
        assert snapshot.total_requests == 3
        assert snapshot.cache_hits == 2
        assert snapshot.semantic_hits == 1
        assert snapshot.cache_misses == 1
        assert snapshot.cache_hits - snapshot.semantic_hits == 1  # exact
    finally:
        metrics.close()

    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 3
        assert summary.cache_hits == 2
        assert summary.semantic_hits == 1
        assert summary.cache_misses == 1
        assert summary.successful_requests == 3
        # Savings reuse the existing avoided-cost logic, not a duplicate:
        # one miss at 0.0007, then both the semantic and the exact hit
        # inherit that avoided cost.
        from decimal import Decimal

        assert summary.estimated_cost_saved == Decimal("0.0014")
        assert summary.estimated_cost == Decimal("0.0007")
        hit_types = {
            row[0]
            for row in sqlite3.connect(db).execute(
                "SELECT hit_type FROM analytics_events "
                "WHERE event_type = 'cache_hit'"
            ).fetchall()
        }
        assert hit_types == {"exact", "semantic"}
    finally:
        store.close()


def test_analytics_migration_for_old_databases(tmp_path):
    """Pre-semantic analytics files gain hit_type without data loss."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                function_name TEXT NOT NULL,
                cache_status TEXT,
                latency_ms REAL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                success INTEGER,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                request_units INTEGER,
                estimated_cost TEXT,
                estimated_cost_saved TEXT
            );
            INSERT INTO analytics_events
                (timestamp, event_type, request_hash, function_name,
                 cache_status, success)
            VALUES ('2026-01-01T00:00:00+00:00', 'cache_hit', 'h', 'f',
                    'hit', 1);
            """
        )
        con.commit()
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 1
        assert summary.cache_hits == 1
        assert summary.semantic_hits == 0  # old rows count as exact history
    finally:
        store.close()


def test_api_semantic_metrics_without_secrets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import create_app

    db = tmp_path / "sem.db"
    embedder = FakeEmbedder(
        {'{"prompt": "A"}': unit(1, 0, 0), '{"prompt": "A too"}': unit(1, 0, 0)}
    )

    @cached_call(
        cache_path=db,
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt):
        return {"answer": prompt}

    generate("A")
    generate("A too")
    app = create_app(cache_db=str(db))
    try:
        with TestClient(app) as client:
            summary = client.get("/api/analytics/summary").json()
            assert summary["semantic_hits"] == 1
            assert summary["cache_hits"] == 1
            stats = client.get("/api/stats").json()
            assert stats["semantic_hits"] == 1
            payload = (
                client.get("/api/analytics/summary").text
                + client.get("/api/stats").text
                + client.get("/api/activity").text
            )
            assert "embedding" not in payload.lower()
            assert "GEMINI_API_KEY" not in payload
    finally:
        app.state.service.close()


# ------------------------------------------------------------------ redis ---


def test_semantic_with_redis_strategy(tmp_path, monkeypatch):
    fake = FakeRedis()
    module = types.ModuleType("redis")

    class FakeFactory:
        @staticmethod
        def from_url(url, **kwargs):
            return fake

    module.Redis = FakeFactory
    monkeypatch.setitem(sys.modules, "redis", module)

    embedder = FakeEmbedder(
        {'{"prompt": "R1"}': unit(1, 0, 0), '{"prompt": "R1 again"}': unit(1, 0, 0)}
    )
    calls = []

    @cached_call(
        strategy="redis",
        redis_url="redis://localhost:6379/0",
        cache_path=tmp_path / "sem_redis.db",
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    assert generate("R1") == {"answer": "R1"}
    assert generate("R1 again") == {"answer": "R1"}  # semantic hit via Redis
    assert calls == ["R1"]
    # Exact values live in Redis; semantic rows in the sidecar SQLite file.
    assert len(fake.data) == 1
    store = SemanticStore(tmp_path / "sem_redis.db")
    try:
        assert store.count() == 1
        summary = AnalyticsStore(tmp_path / "sem_redis.db").summary()
        assert summary.semantic_hits == 1
    finally:
        store.close()


# ------------------------------------------------- real model (marked) ---


@pytest.mark.semantic
def test_real_local_embedding_model():
    """Live check with the bundled local model (may download once).

    Skipped when the model cannot be fetched. Asserts only robust
    relative properties — never magic similarity thresholds.
    """
    from llmcachex.semantic import DEFAULT_SEMANTIC_MODEL

    try:
        provider = LocalEmbeddingProvider()
        beginner_a = provider.embed(
            "Explain recursion in Python for beginners."
        )
        beginner_b = provider.embed(
            "Give a beginner-friendly explanation of Python recursion."
        )
        unrelated = provider.embed(
            "Quantum chromodynamics gauge theory Lagrangian density."
        )
    except Exception as exc:
        pytest.skip(f"local embedding model unavailable: {exc}")
    assert provider.model_name() == DEFAULT_SEMANTIC_MODEL
    assert provider.dimension() == len(beginner_a) == 384
    assert cosine_similarity(beginner_a, beginner_a) == pytest.approx(1.0)
    assert cosine_similarity(beginner_a, beginner_b) > cosine_similarity(
        beginner_a, unrelated
    )


# ------------------------------------------------------------- rate limit ---


def test_semantic_hit_bypasses_rate_limiter(tmp_path):
    embedder = FakeEmbedder(
        {'{"prompt": "K1"}': unit(1, 0, 0), '{"prompt": "K1-ish"}': unit(1, 0, 0)}
    )
    calls = []

    @cached_call(
        cache_path=tmp_path / "sem.db",
        semantic=True,
        semantic_threshold=0.9,
        semantic_fields=["prompt"],
        semantic_provider=embedder,
        rate_limit=1,
        rate_period=0.4,
    )
    def generate(prompt):
        calls.append(prompt)
        return {"answer": prompt}

    assert generate("K1") == {"answer": "K1"}  # consumes the single slot
    import time as _time

    start = _time.perf_counter()
    assert generate("K1-ish") == {"answer": "K1"}  # semantic hit: no wait
    assert _time.perf_counter() - start < 0.4
    assert calls == ["K1"]
