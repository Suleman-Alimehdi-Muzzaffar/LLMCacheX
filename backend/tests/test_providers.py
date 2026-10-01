"""Phase 7 tests: provider abstraction, registry, usage model and cost
calculation. Fully offline — no provider is ever contacted."""

from __future__ import annotations

from decimal import Decimal

import pytest

from llmcachex.analytics import PricingConfig, Usage, coerce_usage, estimate_cost
from llmcachex.providers import (
    BaseProviderAdapter,
    ProviderAdapter,
    StaticAdapter,
    UnknownProviderError,
    get_provider,
    list_providers,
    register_provider,
    unregister_provider,
)


class DummyAdapter(BaseProviderAdapter):
    """Minimal adapter for protocol tests (non-network)."""

    def get_provider_name(self) -> str:
        return "dummy"


# ----------------------------------------------------------------- usage ---


def test_usage_validation_rejects_bad_token_counts():
    with pytest.raises(ValueError, match="input_tokens"):
        Usage(input_tokens=-1)
    with pytest.raises(ValueError, match="output_tokens"):
        Usage(output_tokens="100")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="total_tokens"):
        Usage(total_tokens=True)  # type: ignore[arg-type]


def test_usage_from_mapping_and_coerce():
    usage = coerce_usage({"input_tokens": 10, "output_tokens": 5})
    assert isinstance(usage, Usage)
    assert usage.total == 15
    assert coerce_usage(None) is None
    existing = Usage(input_tokens=1)
    assert coerce_usage(existing) is existing
    with pytest.raises(TypeError, match="Usage extractor"):
        coerce_usage(42)


def test_usage_unknown_fields_stay_none():
    usage = Usage()
    assert usage.input_tokens is None
    assert usage.total is None
    assert usage.has_token_data is False


# --------------------------------------------------------------- pricing ---


def test_cost_calculation_is_exact_with_configured_prices():
    """Part 29: 1000 input + 500 output at explicit per-1k prices."""
    pricing = PricingConfig.from_values(
        input_cost_per_1k_tokens="0.003",
        output_cost_per_1k_tokens="0.006",
    )
    usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    cost = pricing.estimate(usage)
    # 1000/1000 * 0.003 + 500/1000 * 0.006 = 0.003 + 0.003
    assert cost == Decimal("0.006")
    assert isinstance(cost, Decimal)


def test_cost_without_pricing_is_unknown_not_zero():
    usage = Usage(input_tokens=1000, output_tokens=500)
    assert estimate_cost(usage, None) is None
    assert PricingConfig().estimate(usage) is None  # no prices configured


def test_cost_without_usage_is_unknown():
    pricing = PricingConfig.from_values("0.003", "0.006")
    assert pricing.estimate(None) is None
    assert pricing.estimate(Usage()) is None  # no token counts


def test_partial_pricing_only_counts_known_sides():
    pricing = PricingConfig.from_values(input_cost_per_1k_tokens="0.004")
    usage = Usage(input_tokens=1000, output_tokens=500)
    assert pricing.estimate(usage) == Decimal("0.004")
    # Input side unknown -> nothing computable -> None.
    assert pricing.estimate(Usage(output_tokens=500)) is None


def test_float_prices_are_coerced_exactly():
    pricing = PricingConfig.from_values(
        0.0025,  # float binary expansion avoided via str()
        output_cost_per_1k_tokens=0.005,
    )
    assert pricing.input_cost_per_1k_tokens == Decimal("0.0025")
    usage = Usage(input_tokens=1000, output_tokens=1000)
    assert pricing.estimate(usage) == Decimal("0.0075")


def test_pricing_rejects_negative_prices():
    with pytest.raises(ValueError, match="input_cost_per_1k_tokens"):
        PricingConfig.from_values(-1, None)


def test_estimate_cost_accepts_mapping_pricing():
    usage = Usage(input_tokens=1000, output_tokens=1000)
    cost = estimate_cost(
        usage,
        {
            "input_cost_per_1k_tokens": "0.002",
            "output_cost_per_1k_tokens": "0.004",
        },
    )
    assert cost == Decimal("0.006")


# -------------------------------------------------------------- registry ---


def test_register_and_get_provider_is_case_insensitive():
    adapter = DummyAdapter()
    register_provider("Dummy", adapter)
    try:
        assert get_provider("dummy") is adapter
        assert get_provider("DUMMY") is adapter
        assert list_providers() == ["dummy"]
    finally:
        assert unregister_provider("dummy") is True


def test_unknown_provider_raises():
    with pytest.raises(UnknownProviderError, match="No provider registered"):
        get_provider("does-not-exist")
    assert unregister_provider("never-registered") is False


def test_duplicate_registration_requires_replace():
    first, second = DummyAdapter(), DummyAdapter()
    register_provider("dup", first)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_provider("dup", second)
        register_provider("dup", second, replace=True)
        assert get_provider("dup") is second
    finally:
        unregister_provider("dup")


def test_registry_rejects_invalid_names_and_adapters():
    with pytest.raises(ValueError, match="non-empty"):
        register_provider("  ", DummyAdapter())
    with pytest.raises(ValueError, match="ProviderAdapter"):
        register_provider("bad", object())  # type: ignore[arg-type]


def test_static_adapter_is_a_valid_protocol_and_offline():
    adapter = StaticAdapter(
        "local",
        model="local-7b",
        usage_extractor=lambda response: (
            response.get("usage") if isinstance(response, dict) else None
        ),
        pricing=PricingConfig.from_values("0.001", "0.002"),
    )
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.get_provider_name() == "local"
    assert adapter.get_model_name() == "local-7b"
    usage = adapter.extract_usage(
        {"usage": {"input_tokens": 3, "output_tokens": 4}}
    )
    assert usage == Usage(input_tokens=3, output_tokens=4)
    # 3/1000 * 0.001 + 4/1000 * 0.002 = 0.000003 + 0.000008
    assert adapter.estimate_cost(usage) == Decimal("0.000011")
    # Response without usage -> None (never estimated).
    assert adapter.extract_usage(None) is None
    assert adapter.extract_usage({"nope": True}) is None


def test_base_adapter_defaults_are_unknown():
    adapter = BaseProviderAdapter()
    assert adapter.get_model_name() is None
    assert adapter.extract_usage(object()) is None
    assert adapter.estimate_cost(Usage(input_tokens=100)) is None


def test_static_adapter_requires_provider_name():
    with pytest.raises(ValueError, match="non-empty string"):
        StaticAdapter("")
