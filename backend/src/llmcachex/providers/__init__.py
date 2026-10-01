"""Provider abstraction.

The generic base (``base``) and ``registry`` load eagerly — they are
stdlib-only and provider-neutral. Concrete network adapters such as
``gemini`` (which needs the optional ``google-genai`` package) load
lazily on first attribute access, so importing this package never
requires any provider SDK::

    from llmcachex.providers import GeminiProvider  # imports .gemini here
"""

from __future__ import annotations

from typing import Any

from .base import BaseProviderAdapter, ProviderAdapter, StaticAdapter
from .registry import (
    UnknownProviderError,
    get_provider,
    list_providers,
    register_provider,
    unregister_provider,
)

__all__ = [
    "BaseProviderAdapter",
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_MODEL",
    "GeminiConfigurationError",
    "GeminiProvider",
    "GeminiProviderError",
    "ProviderAdapter",
    "StaticAdapter",
    "UnknownProviderError",
    "get_provider",
    "list_providers",
    "register_provider",
    "unregister_provider",
]

#: Names served lazily from ``llmcachex.providers.gemini`` (PEP 562), so
#: the optional ``google-genai`` dependency is never needed at import time.
_LAZY_GEMINI_EXPORTS = frozenset(
    {
        "DEFAULT_MODEL",
        "ENV_API_KEY",
        "ENV_MODEL",
        "GeminiConfigurationError",
        "GeminiProvider",
        "GeminiProviderError",
    }
)


def __getattr__(name: str) -> Any:
    """Load optional provider adapters on first use."""
    if name in _LAZY_GEMINI_EXPORTS:
        from . import gemini

        return getattr(gemini, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )
