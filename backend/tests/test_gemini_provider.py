"""Phase 10 tests: Gemini provider adapter + cached Gemini integration.

Fully offline — the real Gemini API is never contacted. A fake client
stands in for ``google.genai``; one separate manual script (not part of
pytest) exercises the live API.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from llmcachex import cached_call
from llmcachex.analytics import (
    AnalyticsStore,
    PricingConfig,
    Usage,
)
from llmcachex.decorator import _build_request_data
from llmcachex.providers.gemini import (
    DEFAULT_MODEL,
    GeminiConfigurationError,
    GeminiProvider,
    GeminiProviderError,
)


# ------------------------------------------------------------------ fakes ---


class FakeUsageMetadata:
    """Mimics ``GenerateContentResponseUsageMetadata`` (real field names)."""

    def __init__(self, prompt=10, candidates=20, total=30):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total


class FakeResponse:
    """Mimics ``GenerateContentResponse`` (``.text`` + ``usage_metadata``)."""

    def __init__(self, text="hello", usage=None):
        self._text = text
        self.usage_metadata = usage

    @property
    def text(self):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class FakeModels:
    """Scripted ``client.models`` namespace."""

    def __init__(self):
        self.calls: list[dict] = []
        self.script: list = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(
            {"model": model, "contents": contents, "config": config}
        )
        if not self.script:
            return FakeResponse(
                text=f"answer:{contents}",
                usage=FakeUsageMetadata(),
            )
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class FakeClient:
    """Injectable stand-in for ``genai.Client`` (no key, no network)."""

    def __init__(self):
        self.models = FakeModels()


def make_provider(**kwargs):
    """GeminiProvider wired to a fake client (never needs a real key)."""
    client = kwargs.pop("client", FakeClient())
    return GeminiProvider(client=client, **kwargs), client


# ------------------------------------------------------- init and config ---


def test_default_model_and_metadata(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider, _ = make_provider()
    assert provider.get_model_name() == DEFAULT_MODEL
    assert provider.get_provider_name() == "gemini"


def test_explicit_model_wins_over_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-env-model")
    provider, _ = make_provider(model="gemini-picked")
    assert provider.get_model_name() == "gemini-picked"


def test_env_model_used_when_no_explicit_model(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-env-model")
    provider, _ = make_provider()
    assert provider.get_model_name() == "gemini-env-model"


def test_empty_model_rejected():
    with pytest.raises(GeminiConfigurationError, match="non-empty string"):
        GeminiProvider(model="  ", client=FakeClient())


def test_init_needs_no_key_and_no_sdk(monkeypatch):
    # Constructing the provider must never require a key or the SDK.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider(model="m", client=FakeClient())
    assert provider.get_model_name() == "m"


def test_repr_never_contains_key():
    provider = GeminiProvider(model="m", api_key="sk-fake-123", client=FakeClient())
    assert "sk-fake-123" not in repr(provider)
    assert "m" in repr(provider)


# ------------------------------------------------------------- missing key ---


def test_generate_without_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider(model="m")  # no client, no key
    with pytest.raises(GeminiConfigurationError, match="GEMINI_API_KEY"):
        provider.generate("hi")


def test_explicit_key_is_used(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider(
        model="m", api_key="explicit-key", client=FakeClient()
    )
    # Key resolution succeeds (fake client means no network use).
    assert provider.generate("hi") == "answer:hi"


def test_missing_sdk_gives_install_hint(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.setitem(sys.modules, "google", None)
    provider = GeminiProvider(model="m", api_key="env-key")
    with pytest.raises(GeminiConfigurationError, match="llmcachex\\[gemini\\]"):
        provider.generate("hi")


# --------------------------------------------------------------- generate ---


def test_generate_returns_text_and_records_model():
    provider, client = make_provider(model="gemini-test")
    assert provider.generate("Explain caching") == "answer:Explain caching"
    assert client.models.calls[0]["model"] == "gemini-test"
    assert client.models.calls[0]["contents"] == "Explain caching"


def test_generate_rejects_bad_prompt():
    provider, _ = make_provider()
    with pytest.raises(GeminiConfigurationError, match="non-empty string"):
        provider.generate("  ")
    with pytest.raises(GeminiConfigurationError, match="non-empty string"):
        provider.generate(None)  # type: ignore[arg-type]


def test_generate_empty_text_raises():
    client = FakeClient()
    client.models.script.append(FakeResponse(text="", usage=None))
    provider = GeminiProvider(model="m", client=client)
    with pytest.raises(GeminiProviderError, match="empty response"):
        provider.generate("hi")


def test_generate_text_failure_raises():
    client = FakeClient()
    client.models.script.append(
        FakeResponse(text=ValueError("no parts"), usage=None)
    )
    provider = GeminiProvider(model="m", client=client)
    with pytest.raises(GeminiProviderError, match="no text"):
        provider.generate("hi")


# ----------------------------------------------------------- error mapping ---


def test_sdk_errors_become_provider_errors():
    client = FakeClient()
    client.models.script.append(RuntimeError("429 rate limited"))
    provider = GeminiProvider(model="m", client=client)
    with pytest.raises(GeminiProviderError, match="429 rate limited") as exc_info:
        provider.generate("hi")
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_api_key_redacted_from_error_messages():
    client = FakeClient()
    client.models.script.append(Exception("invalid key sk-fake-123 rejected"))
    provider = GeminiProvider(
        model="m", api_key="sk-fake-123", client=client
    )
    with pytest.raises(GeminiProviderError) as exc_info:
        provider.generate("hi")
    assert "sk-fake-123" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_config_errors_are_not_wrapped_as_provider_errors(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider(model="m")  # missing key, no client
    with pytest.raises(GeminiConfigurationError, match="GEMINI_API_KEY"):
        provider.generate("hi")


# -------------------------------------------------------- usage extraction ---


def test_extract_usage_from_sdk_response():
    provider, _ = make_provider()
    usage = provider.extract_usage(FakeResponse(usage=FakeUsageMetadata(7, 8, 15)))
    assert usage == Usage(input_tokens=7, output_tokens=8, total_tokens=15)


def test_extract_usage_none_when_unavailable():
    provider, _ = make_provider()
    assert provider.extract_usage(FakeResponse(usage=None)) is None
    assert provider.extract_usage(None) is None
    assert provider.extract_usage(object()) is None


def test_generate_stashes_usage_for_text_results():
    provider, _ = make_provider()
    provider.generate("hi")
    assert provider.extract_usage("answer:hi") == Usage(
        input_tokens=10, output_tokens=20, total_tokens=30
    )


def test_extract_usage_accepts_usage_and_mappings():
    provider, _ = make_provider()
    existing = Usage(input_tokens=1)
    assert provider.extract_usage(existing) is existing
    assert provider.extract_usage({"input_tokens": 4}) == Usage(input_tokens=4)


# ------------------------------------------------------------------- cost ---


def test_estimate_cost_none_without_pricing():
    provider, _ = make_provider()
    assert provider.estimate_cost(Usage(input_tokens=100)) is None


def test_estimate_cost_with_configured_pricing():
    pricing = PricingConfig.from_values(0.01, 0.03)
    provider, _ = make_provider(pricing=pricing)
    # 100/1000 * 0.01 + 200/1000 * 0.03 = 0.001 + 0.006
    assert provider.estimate_cost(
        Usage(input_tokens=100, output_tokens=200)
    ) == Decimal("0.007")


# ------------------------------------------------------- cache identity ---


def test_provider_identity_joins_request_data():
    def f(x):
        return x

    plain = _build_request_data(f, {"x": 1})
    assert "provider" not in plain  # backward compatible: key omitted
    identified = _build_request_data(
        f, {"x": 1}, provider="gemini", model="gemini-3.8-flash"
    )
    assert identified["provider"] == {
        "name": "gemini",
        "model": "gemini-3.8-flash",
    }
    other = _build_request_data(f, {"x": 1}, provider="gemini", model="other")
    assert identified != other


# ----------------------------------------------- cached gemini integration ---


def test_cache_miss_calls_provider_and_hit_does_not(tmp_path):
    db = tmp_path / "gemini.db"
    provider, client = make_provider(model="gemini-test")
    pricing = PricingConfig.from_values(0.01, 0.03)

    @cached_call(cache_path=db, provider=provider, pricing=pricing)
    def generate(prompt):
        return provider.generate(prompt)

    assert generate("What is Python?") == "answer:What is Python?"
    assert len(client.models.calls) == 1
    # Second identical call: served from cache, provider untouched.
    assert generate("What is Python?") == "answer:What is Python?"
    assert len(client.models.calls) == 1


def test_different_models_do_not_share_entries(tmp_path):
    db = tmp_path / "gemini.db"
    provider_a, client_a = make_provider(model="model-a")
    provider_b, client_b = make_provider(model="model-b")
    active = {"provider": provider_a}

    def shared(prompt):
        return active["provider"].generate(prompt)

    gen_a = cached_call(cache_path=db, provider=provider_a)(shared)
    gen_b = cached_call(cache_path=db, provider=provider_b)(shared)

    # Same function, same arguments, model A: a miss on first use...
    assert gen_a("same") == "answer:same"
    assert len(client_a.models.calls) == 1
    assert gen_a("same") == "answer:same"
    assert len(client_a.models.calls) == 1
    # ...but model B must NOT reuse model A's entry.
    active["provider"] = provider_b
    assert gen_b("same") == "answer:same"
    assert len(client_b.models.calls) == 1
    assert gen_b("same") == "answer:same"
    assert len(client_b.models.calls) == 1
    # And model A's entry is still intact afterwards.
    active["provider"] = provider_a
    assert gen_a("same") == "answer:same"
    assert len(client_a.models.calls) == 1


def test_ttl_expiry_calls_provider_again(tmp_path):
    db = tmp_path / "gemini.db"
    provider, client = make_provider()

    @cached_call(cache_path=db, provider=provider, ttl_seconds=3600)
    def generate(prompt):
        return provider.generate(prompt)

    assert generate("hi") == "answer:hi"
    assert generate("hi") == "answer:hi"
    assert len(client.models.calls) == 1
    # Expire the row directly (no slow sleeps in tests).
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE cache_entries SET expires_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )
        con.commit()
    assert generate("hi") == "answer:hi"
    assert len(client.models.calls) == 2


def test_retry_then_success_caches_only_success(tmp_path):
    db = tmp_path / "gemini.db"
    client = FakeClient()
    client.models.script.append(RuntimeError("boom 1"))
    client.models.script.append(RuntimeError("boom 2"))
    provider = GeminiProvider(model="m", client=client)

    @cached_call(cache_path=db, provider=provider, retries=2, backoff_factor=0)
    def generate(prompt):
        return provider.generate(prompt)

    assert generate("hi") == "answer:hi"
    assert len(client.models.calls) == 3  # 2 failures + 1 success
    # Success is cached; failures were never cached.
    assert generate("hi") == "answer:hi"
    assert len(client.models.calls) == 3


def test_rate_limit_hits_bypass_misses_consume(tmp_path):
    db = tmp_path / "gemini.db"
    provider, client = make_provider()

    @cached_call(
        cache_path=db, provider=provider, rate_limit=1, rate_period=0.4
    )
    def generate(prompt):
        return provider.generate(prompt)

    assert generate("one") == "answer:one"  # miss: consumes the 1 slot
    assert generate("two") == "answer:two"  # miss: waits, then executes
    assert len(client.models.calls) == 2
    # The window is now full, but a cache hit must not wait or execute.
    import time

    start = time.perf_counter()
    assert generate("one") == "answer:one"
    elapsed = time.perf_counter() - start
    assert len(client.models.calls) == 2
    assert elapsed < 0.4


def test_gemini_analytics_records_provider_model_and_usage(tmp_path):
    db = tmp_path / "gemini.db"
    provider, _ = make_provider(model="gemini-analytics")
    pricing = PricingConfig.from_values(0.01, 0.03)

    @cached_call(cache_path=db, provider=provider, pricing=pricing)
    def generate(prompt):
        return provider.generate(prompt)

    generate("hi")  # miss
    generate("hi")  # hit

    store = AnalyticsStore(db)
    try:
        rows = store.providers()
        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "gemini"
        assert row.requests == 2
        assert row.cache_hits == 1
        assert row.cache_misses == 1
        assert row.total_tokens == 30  # provider-reported, not estimated
        totals = store.usage_totals()
        assert totals.input_tokens == 10
        assert totals.output_tokens == 20
        assert totals.total_tokens == 30
        assert totals.estimated_cost == Decimal("0.0007")
        # 10/1000*0.01 + 20/1000*0.03 = 0.0001 + 0.0006
        assert totals.estimated_cost_saved == Decimal("0.0007")
        summary = store.summary()
        assert summary.total_requests == 2
        assert summary.successful_requests == 2
    finally:
        store.close()


def test_gemini_stored_data_carries_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-value-abc123")
    db = tmp_path / "gemini.db"
    provider = GeminiProvider(model="m", client=FakeClient())

    @cached_call(cache_path=db, provider=provider)
    def generate(prompt):
        return provider.generate(prompt)

    generate("tell me a secret")
    with sqlite3.connect(db) as con:
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        for table in tables:
            columns = [
                row[1]
                for row in con.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            ]
            for column in columns:
                for (value,) in con.execute(
                    f"SELECT {column} FROM {table}"
                ).fetchall():
                    text = "" if value is None else str(value)
                    assert "test-key-value-abc123" not in text
                    assert "GEMINI_API_KEY" not in text


# ------------------------------------------------- hardening (Phase 11) ---


def test_retry_counts_single_request_not_three(tmp_path):
    """Part 15: 1 logical request failing twice then succeeding."""
    db = tmp_path / "gemini.db"
    client = FakeClient()
    client.models.script.append(ConnectionError("down 1"))
    client.models.script.append(ConnectionError("down 2"))
    provider = GeminiProvider(model="retry-model", client=client)

    @cached_call(cache_path=db, provider=provider, retries=2, backoff_factor=0)
    def generate(prompt):
        return provider.generate(prompt)

    assert generate("hi") == "answer:hi"
    assert len(client.models.calls) == 3
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 1  # retries are not requests
        assert summary.retry_count == 2
        assert summary.successful_requests == 1
        assert summary.failed_requests == 0
    finally:
        store.close()


def test_all_attempts_failing_caches_nothing(tmp_path):
    db = tmp_path / "gemini.db"
    provider, _ = make_provider(model="fail-model")
    failures = {"n": 0}

    @cached_call(cache_path=db, provider=provider, retries=2, backoff_factor=0)
    def generate(prompt):
        failures["n"] += 1
        raise ConnectionError("always down")

    with pytest.raises(ConnectionError, match="always down"):
        generate("hi")
    assert failures["n"] == 3
    with sqlite3.connect(db) as con:
        count = con.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    assert count == 0
    store = AnalyticsStore(db)
    try:
        summary = store.summary()
        assert summary.total_requests == 1
        assert summary.failed_requests == 1
        assert summary.retry_count == 2
    finally:
        store.close()
    # Nothing cached: a later call re-executes instead of hitting.
    with pytest.raises(ConnectionError):
        generate("hi")
    assert failures["n"] == 6


def test_api_key_not_part_of_cache_identity(tmp_path):
    """Same model + different keys must share one entry (key never hashed)."""
    db = tmp_path / "gemini.db"
    provider_a, _ = make_provider(model="same-model", api_key="key-A")
    provider_b, client_b = make_provider(model="same-model", api_key="key-B")
    active = {"p": provider_a}

    def shared(prompt):
        return active["p"].generate(prompt)

    gen_a = cached_call(cache_path=db, provider=provider_a)(shared)
    gen_b = cached_call(cache_path=db, provider=provider_b)(shared)

    assert gen_a("same") == "answer:same"
    active["p"] = provider_b
    before = len(client_b.models.calls)
    # Only the key differs: this must be a HIT, not a new execution.
    assert gen_b("same") == "answer:same"
    assert len(client_b.models.calls) == before


def test_gemini_registry_roundtrip():
    from llmcachex.providers import (
        UnknownProviderError,
        get_provider,
        list_providers,
        register_provider,
        unregister_provider,
    )

    provider, _ = make_provider(model="reg-model")
    try:
        register_provider("gemini-phase11", provider)
        assert get_provider("gemini-phase11") is provider
        assert get_provider("GEMINI-PHASE11") is provider  # case-insensitive
        assert "gemini-phase11" in list_providers()
        with pytest.raises(ValueError, match="already registered"):
            register_provider("gemini-phase11", provider)
        with pytest.raises(UnknownProviderError):
            get_provider("no-such-provider-11")
    finally:
        assert unregister_provider("gemini-phase11") is True
    assert "gemini-phase11" not in list_providers()


def test_analytics_events_carry_configured_model(tmp_path):
    """Events must record the actual configured model, not a default."""
    db = tmp_path / "gemini.db"
    provider, _ = make_provider(model="model-under-test")

    @cached_call(cache_path=db, provider=provider)
    def generate(prompt):
        return provider.generate(prompt)

    generate("hi")  # miss
    generate("hi")  # hit inherits the prior model label
    with sqlite3.connect(db) as con:
        labels = set(
            con.execute(
                "SELECT provider, model FROM analytics_events"
            ).fetchall()
        )
    assert labels == {("gemini", "model-under-test")}


def test_core_usable_with_google_sdk_blocked():
    """Hermetic proof: core + provider package need no google-genai."""
    code = (
        "import sys, tempfile, os\n"
        "sys.modules['google'] = None\n"
        "sys.modules['google.genai'] = None\n"
        "import llmcachex.providers\n"
        "assert 'llmcachex.providers.gemini' not in sys.modules\n"
        "from llmcachex import cached_call\n"
        "from llmcachex.providers.gemini import GeminiProvider\n"
        "db = os.path.join(tempfile.mkdtemp(), 'c.db')\n"
        "calls = []\n"
        "def raw(x):\n"
        "    calls.append(x)\n"
        "    return x * 2\n"
        "f = cached_call(cache_path=db)(raw)\n"
        "assert f(21) == 42 and f(21) == 42 and calls == [21]\n"
        "p = GeminiProvider(model='m', client=object())\n"
        "assert (p.get_provider_name(), p.get_model_name()) == "
        "('gemini', 'm')\n"
        "print('CORE-OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "CORE-OK" in proc.stdout
