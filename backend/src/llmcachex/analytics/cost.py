"""Configurable cost estimation (no hardcoded provider pricing).

Prices are *data*, supplied by the developer or a provider adapter:
LLMCacheX never ships or claims current real-world pricing. All money
arithmetic uses :class:`decimal.Decimal` to avoid binary floating-point
rounding in cost-style calculations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .usage import Usage

__all__ = ["PricingConfig", "estimate_cost", "coerce_decimal"]

_TOKENS_PER_1K = Decimal(1000)


def coerce_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Convert ``int | float | str | Decimal`` to an exact ``Decimal``.

    Floats are routed through ``str()`` first so that e.g. ``0.0025``
    becomes ``Decimal("0.0025")`` rather than the binary expansion of
    the float.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be a number, got {value!r}.")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a valid number: {value!r}.") from exc


def _check_price(name: str, value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}.")
    return value


@dataclass(frozen=True)
class PricingConfig:
    """Prices per 1000 tokens as configured data.

    ``None`` means "price unknown". The configuration is intentionally
    explicit: no default prices are bundled with the library, so an
    estimate is only produced when the developer supplied pricing.

    Args:
        input_cost_per_1k_tokens: Price for 1000 input tokens, or None.
        output_cost_per_1k_tokens: Price for 1000 output tokens, or None.
        currency: Optional label (e.g. ``"USD"``) for display only.
    """

    input_cost_per_1k_tokens: Decimal | None = None
    output_cost_per_1k_tokens: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _check_price("input_cost_per_1k_tokens", self.input_cost_per_1k_tokens)
        _check_price("output_cost_per_1k_tokens", self.output_cost_per_1k_tokens)

    @classmethod
    def from_values(
        cls,
        input_cost_per_1k_tokens: Any = None,
        output_cost_per_1k_tokens: Any = None,
        currency: str | None = None,
    ) -> PricingConfig:
        """Build from plain numbers/strings (converted to Decimal safely)."""
        return cls(
            input_cost_per_1k_tokens=(
                None
                if input_cost_per_1k_tokens is None
                else coerce_decimal(
                    input_cost_per_1k_tokens,
                    field="input_cost_per_1k_tokens",
                )
            ),
            output_cost_per_1k_tokens=(
                None
                if output_cost_per_1k_tokens is None
                else coerce_decimal(
                    output_cost_per_1k_tokens,
                    field="output_cost_per_1k_tokens",
                )
            ),
            currency=currency,
        )

    def estimate(self, usage: Usage | None) -> Decimal | None:
        """Estimate cost for ``usage``; ``None`` when it cannot be derived.

        Each side (input/output) contributes only when both its price and
        its token count are known. If no side is computable the result is
        ``None`` — never zero — so "unknown" stays distinguishable from
        "free".
        """
        if usage is None:
            return None
        parts: list[Decimal] = []
        if (
            self.input_cost_per_1k_tokens is not None
            and usage.input_tokens is not None
        ):
            parts.append(
                Decimal(usage.input_tokens)
                / _TOKENS_PER_1K
                * self.input_cost_per_1k_tokens
            )
        if (
            self.output_cost_per_1k_tokens is not None
            and usage.output_tokens is not None
        ):
            parts.append(
                Decimal(usage.output_tokens)
                / _TOKENS_PER_1K
                * self.output_cost_per_1k_tokens
            )
        if not parts:
            return None
        return sum(parts, Decimal(0))


def estimate_cost(
    usage: Usage | None, pricing: PricingConfig | Mapping[str, Any] | None
) -> Decimal | None:
    """Estimate cost from usage + pricing, tolerating unknown inputs."""
    if pricing is None:
        return None
    if isinstance(pricing, Mapping):
        pricing = PricingConfig(
            input_cost_per_1k_tokens=(
                None
                if pricing.get("input_cost_per_1k_tokens") is None
                else coerce_decimal(
                    pricing["input_cost_per_1k_tokens"],
                    field="input_cost_per_1k_tokens",
                )
            ),
            output_cost_per_1k_tokens=(
                None
                if pricing.get("output_cost_per_1k_tokens") is None
                else coerce_decimal(
                    pricing["output_cost_per_1k_tokens"],
                    field="output_cost_per_1k_tokens",
                )
            ),
            currency=pricing.get("currency"),
        )
    return pricing.estimate(usage)
