"""Google Gemini provider adapter (first real LLM provider).

This module is the ONLY place with Gemini-specific logic: client setup,
request dispatch, response text extraction and usage parsing. It never
touches SQLite, hashing, TTL, retry or rate limiting — those stay in the
provider-independent core (``@cached_call``).

Requires the optional dependency (never a core requirement)::

    pip install "llmcachex[gemini]"

The API key is read from the ``GEMINI_API_KEY`` environment variable
(or passed explicitly); it is never logged, never stored in analytics,
and never included in error messages or the cache key.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from ..analytics.cost import PricingConfig, estimate_cost
from ..analytics.usage import Usage, coerce_usage
from .base import BaseProviderAdapter

__all__ = [
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_MODEL",
    "GeminiConfigurationError",
    "GeminiProvider",
    "GeminiProviderError",
]

#: Default model used when neither an explicit model nor ``GEMINI_MODEL``
#: is configured.
DEFAULT_MODEL: str = "gemini-3.8-flash"

#: Environment variable carrying the Gemini API key (never hardcoded).
ENV_API_KEY: str = "GEMINI_API_KEY"

#: Optional environment variable overriding the default model.
ENV_MODEL: str = "GEMINI_MODEL"


class GeminiConfigurationError(ValueError):
    """Raised when the Gemini provider is misconfigured.

    Most commonly: no API key is available. The message never contains
    the key itself.
    """


class GeminiProviderError(RuntimeError):
    """Raised when a Gemini request fails (network, model, SDK error).

    The message preserves useful context but never contains the API key.
    """


class GeminiProvider(BaseProviderAdapter):
    """Adapter for Google Gemini via the official ``google-genai`` SDK.

    Args:
        model: Model to request (e.g. ``"gemini-3.8-flash"``). Defaults
            to the ``GEMINI_MODEL`` environment variable, else
            :data:`DEFAULT_MODEL`.
        api_key: API key to use instead of ``GEMINI_API_KEY``. Prefer the
            environment variable; pass explicitly only in tests/tools.
        client: Pre-built ``genai.Client`` (or test double). When given,
            no API key is required and the SDK is never imported.
        pricing: Optional :class:`~llmcachex.analytics.PricingConfig`
            used by :meth:`estimate_cost`. Unknown unless configured.
        config: Optional extra generation config forwarded verbatim to
            ``client.models.generate_content``.

    The SDK client is created lazily on the first :meth:`generate` call,
    so merely importing this module or constructing the provider never
    requires a key or the ``google-genai`` package.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        pricing: PricingConfig | None = None,
        config: Any | None = None,
    ) -> None:
        resolved_model = (
            model
            if model is not None
            else os.environ.get(ENV_MODEL, DEFAULT_MODEL)
        )
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise GeminiConfigurationError(
                "Gemini model must be a non-empty string."
            )
        self._model = resolved_model.strip()
        self._api_key = api_key
        self._client = client
        self._pricing = pricing
        self._config = config
        self._local = threading.local()

    def __repr__(self) -> str:
        # Never include the API key.
        return f"{type(self).__name__}(model={self._model!r})"

    # ------------------------------------------------------------ identity ---

    def get_provider_name(self) -> str:
        """Stable provider identifier for analytics and cache identity."""
        return "gemini"

    def get_model_name(self) -> str | None:
        """Configured model label."""
        return self._model

    # -------------------------------------------------------------- client ---

    def _resolve_api_key(self) -> str:
        """API key from explicit value or environment (never logged)."""
        key = (
            self._api_key
            if self._api_key is not None
            else os.environ.get(ENV_API_KEY)
        )
        if not isinstance(key, str) or not key.strip():
            raise GeminiConfigurationError(
                "Gemini API key is not configured. Set GEMINI_API_KEY "
                "in the backend environment."
            )
        return key.strip()

    def _ensure_client(self) -> Any:
        """Build the SDK client on first use (lazy, key required)."""
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise GeminiConfigurationError(
                'The "google-genai" package is required to use '
                'GeminiProvider. Install it with '
                'pip install "llmcachex[gemini]".'
            ) from exc
        self._client = genai.Client(api_key=self._resolve_api_key())
        return self._client

    def _sanitize(self, message: str) -> str:
        """Redact any accidental key occurrence in an error message."""
        key = self._api_key or os.environ.get(ENV_API_KEY, "")
        if key and key in message:
            message = message.replace(key, "[REDACTED]")
        return message

    # ------------------------------------------------------------ generate ---

    def generate(self, prompt: str) -> str:
        """Generate a text response for ``prompt`` via the Gemini API.

        Returns:
            The model's response text.

        Raises:
            GeminiConfigurationError: Missing key/SDK or bad arguments.
            GeminiProviderError: API, network, model or response errors.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise GeminiConfigurationError("prompt must be a non-empty string.")
        client = self._ensure_client()
        try:
            if self._config is None:
                response = client.models.generate_content(
                    model=self._model, contents=prompt
                )
            else:
                response = client.models.generate_content(
                    model=self._model, contents=prompt, config=self._config
                )
        except GeminiConfigurationError:
            raise
        except GeminiProviderError:
            raise
        except Exception as exc:
            raise GeminiProviderError(
                self._sanitize(f"Gemini request failed: {exc}")
            ) from exc
        usage = self.extract_usage(response)
        # Stash per-thread so the decorator's post-execution usage hook
        # can attribute provider-reported tokens to this text result.
        self._local.last_usage = usage
        try:
            text = response.text
        except Exception as exc:
            raise GeminiProviderError(
                self._sanitize(f"Gemini returned no text: {exc}")
            ) from exc
        if not isinstance(text, str) or not text:
            raise GeminiProviderError("Gemini returned an empty response.")
        return text

    # --------------------------------------------------------------- usage ---

    def extract_usage(self, response: Any) -> Usage | None:
        """Pull provider-reported token counts out of a Gemini response.

        Accepts a raw SDK response (parsed from ``usage_metadata``), a
        :class:`Usage`, a mapping, or ``None``. For any other value
        (e.g. the plain text returned by :meth:`generate`), the usage
        stashed by the most recent :meth:`generate` call on this thread
        is returned. Returns ``None`` when no usage is available — usage
        is never estimated or fabricated.
        """
        if response is None:
            return None
        if isinstance(response, Usage):
            return response
        if isinstance(response, Mapping):
            return coerce_usage(response)
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata is not None:
            return Usage(
                input_tokens=getattr(usage_metadata, "prompt_token_count", None),
                output_tokens=getattr(
                    usage_metadata, "candidates_token_count", None
                ),
                total_tokens=getattr(
                    usage_metadata, "total_token_count", None
                ),
            )
        if isinstance(response, str):
            return getattr(self._local, "last_usage", None)
        return None

    def estimate_cost(self, usage: Usage) -> Decimal | None:
        """Estimate cost from configured pricing; ``None`` when unpriced."""
        return estimate_cost(usage, self._pricing)
