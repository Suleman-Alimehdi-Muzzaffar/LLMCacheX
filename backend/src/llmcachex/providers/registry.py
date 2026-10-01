"""In-process registry of provider adapters.

Future provider integrations register themselves here so application
code can look them up by name without importing concrete adapters
everywhere. Registration is pure bookkeeping — no network activity, no
API keys.
"""

from __future__ import annotations

import threading

from .base import ProviderAdapter

__all__ = [
    "UnknownProviderError",
    "register_provider",
    "get_provider",
    "unregister_provider",
    "list_providers",
]


class UnknownProviderError(LookupError):
    """Raised when no adapter is registered under the requested name."""


_registry: dict[str, ProviderAdapter] = {}
_registry_lock = threading.Lock()


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Provider name must be a non-empty string.")
    return name.strip().lower()


def register_provider(
    name: str, adapter: ProviderAdapter, *, replace: bool = False
) -> None:
    """Register ``adapter`` under ``name`` (case-insensitive).

    Args:
        name: Lookup key, e.g. ``"openai"`` (normalized to lowercase).
        adapter: Object satisfying the :class:`ProviderAdapter` protocol.
        replace: Allow overwriting an existing registration.

    Raises:
        ValueError: Invalid name, adapter missing protocol methods, or
            the name is taken and ``replace`` is False.
    """
    key = _validate_name(name)
    if not isinstance(adapter, ProviderAdapter):
        raise ValueError(
            "adapter must implement get_provider_name, get_model_name, "
            "extract_usage and estimate_cost (ProviderAdapter protocol)."
        )
    with _registry_lock:
        if key in _registry and not replace:
            raise ValueError(
                f"Provider {key!r} is already registered; "
                "pass replace=True to overwrite."
            )
        _registry[key] = adapter


def get_provider(name: str) -> ProviderAdapter:
    """Return the adapter registered under ``name``.

    Raises:
        UnknownProviderError: If nothing is registered for ``name``.
    """
    key = _validate_name(name)
    with _registry_lock:
        adapter = _registry.get(key)
    if adapter is None:
        raise UnknownProviderError(
            f"No provider registered under {key!r}. "
            "Register one with register_provider() first."
        )
    return adapter


def unregister_provider(name: str) -> bool:
    """Remove a registration; ``True`` when one existed."""
    key = _validate_name(name)
    with _registry_lock:
        return _registry.pop(key, None) is not None


def list_providers() -> list[str]:
    """Sorted names of all registered providers."""
    with _registry_lock:
        return sorted(_registry)
