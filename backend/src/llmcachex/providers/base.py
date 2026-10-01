"""Provider adapter interface (abstraction only — no network calls).

A *provider adapter* knows three things: which provider/model it stands
for, how to pull :class:`~llmcachex.analytics.usage.Usage` out of a
response object, and how to price that usage when pricing is configured.
It never performs I/O, needs no API key, and is safe to construct in any
environment.

Real provider clients (OpenAI, Anthropic, Gemini, ...) are intentionally
NOT implemented in this phase — only this interface.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from ..analytics.cost import PricingConfig, estimate_cost
from ..analytics.usage import Usage, coerce_usage

__all__ = ["ProviderAdapter", "BaseProviderAdapter", "StaticAdapter"]


@runtime_checkable
class ProviderAdapter(Protocol):
    """Structural interface every future provider adapter can satisfy."""

    def get_provider_name(self) -> str:
        """Stable provider identifier (e.g. ``"openai"``)."""
        ...

    def get_model_name(self) -> str | None:
        """Model this adapter targets, or ``None`` when not applicable."""
        ...

    def extract_usage(self, response: Any) -> Usage | None:
        """Pull provider-reported usage out of a response object.

        Returns ``None`` when the response carries no usage metadata.
        Must never estimate or infer token counts.
        """
        ...

    def estimate_cost(self, usage: Usage) -> Decimal | None:
        """Estimate the cost of ``usage`` from configured pricing.

        Returns ``None`` when pricing or usage is unknown — never a
        fabricated number.
        """
        ...


class BaseProviderAdapter:
    """Convenience base class with safe defaults.

    Subclasses only override what they need: ``get_provider_name`` is
    required in practice; everything else defaults to "unknown".
    """

    def get_model_name(self) -> str | None:
        """No model by default."""
        return None

    def extract_usage(self, response: Any) -> Usage | None:
        """No automatic usage extraction by default."""
        return None

    def estimate_cost(self, usage: Usage) -> Decimal | None:
        """No pricing by default (unknown cost stays ``None``)."""
        return None


class StaticAdapter(BaseProviderAdapter):
    """Labeled, non-network adapter for architecture use and tests.

    Clearly a placeholder: it performs no I/O. It can carry a static
    provider/model label, an optional usage-extractor callable, and an
    optional :class:`PricingConfig`.

    Args:
        provider: Provider name (e.g. ``"local"``).
        model: Optional model label.
        usage_extractor: Optional callable mapping a response object to
            ``Usage | dict | None``.
        pricing: Optional configured pricing (never bundled defaults).
    """

    def __init__(
        self,
        provider: str,
        *,
        model: str | None = None,
        usage_extractor: Callable[[Any], Usage | dict | None] | None = None,
        pricing: PricingConfig | None = None,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string.")
        self._provider = provider
        self._model = model
        self._usage_extractor = usage_extractor
        self._pricing = pricing

    def get_provider_name(self) -> str:
        """Configured provider label."""
        return self._provider

    def get_model_name(self) -> str | None:
        """Configured model label."""
        return self._model

    def extract_usage(self, response: Any) -> Usage | None:
        """Run the configured extractor, if any; otherwise ``None``."""
        if self._usage_extractor is None:
            return None
        return coerce_usage(self._usage_extractor(response))

    def estimate_cost(self, usage: Usage) -> Decimal | None:
        """Estimate via configured pricing; ``None`` when unpriced."""
        return estimate_cost(usage, self._pricing)
