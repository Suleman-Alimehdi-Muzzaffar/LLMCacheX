"""Embedding abstraction for semantic caching (local inference only).

The rest of LLMCacheX depends on :class:`EmbeddingProvider`, never on
the underlying ML library. :class:`LocalEmbeddingProvider` runs the
default local model entirely on-device via the optional ``fastembed``
dependency — no API key, no network calls at inference time (the model
files are downloaded once on first use and cached locally by the
embedding library).

The model object is loaded lazily on the first :meth:`embed` call and
shared process-wide per model name, so repeated requests never reload
it. Importing this module never touches ML libraries.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_SEMANTIC_MODEL",
    "ENV_SEMANTIC_MODEL",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "SemanticError",
]

#: Default local embedding model (small, general-purpose, MIT-licensed
#: weights). Overridable per decorator or via ``SEMANTIC_MODEL``.
DEFAULT_SEMANTIC_MODEL: str = "BAAI/bge-small-en-v1.5"

#: Optional environment variable overriding the default embedding model.
ENV_SEMANTIC_MODEL: str = "SEMANTIC_MODEL"

#: Expected dimensionality of :data:`DEFAULT_SEMANTIC_MODEL` (recorded
#: with each entry; mismatches are treated as incompatible, never
#: silently compared).
DEFAULT_SEMANTIC_DIMENSION: int = 384

_model_cache: dict[str, Any] = {}
_model_cache_lock = threading.Lock()


class SemanticError(Exception):
    """Semantic caching is misconfigured or its backend failed.

    Raised for missing optional dependencies, unknown models and other
    configuration problems. Messages are actionable and never contain
    secrets (embeddings must never be built from secret material).
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal interface the semantic cache needs from an embedder."""

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` locally; return a non-empty float vector."""
        ...

    def dimension(self) -> int | None:
        """Vector dimension, or ``None`` when not known before embedding."""
        ...

    def model_name(self) -> str:
        """Stable model identifier recorded with each semantic entry."""
        ...


def resolve_model_name(model: str | None) -> str:
    """Explicit model, else ``SEMANTIC_MODEL``, else the default model."""
    if model is not None:
        resolved = model.strip()
        if not resolved:
            raise SemanticError("semantic_model must be a non-empty string.")
        return resolved
    env_model = os.environ.get(ENV_SEMANTIC_MODEL, "").strip()
    return env_model or DEFAULT_SEMANTIC_MODEL


class LocalEmbeddingProvider:
    """Local ONNX embedding provider (optional ``fastembed`` dependency).

    Args:
        model: Embedding model name. Defaults to ``SEMANTIC_MODEL``,
            else :data:`DEFAULT_SEMANTIC_MODEL`.

    The underlying model loads on the first :meth:`embed` call and is
    shared process-wide per model name (thread-safe). Constructing this
    class never loads anything.
    """

    def __init__(self, model: str | None = None) -> None:
        self._model_name = resolve_model_name(model)
        self._dimension: int | None = None

    def model_name(self) -> str:
        """Configured model identifier."""
        return self._model_name

    def dimension(self) -> int | None:
        """Known vector dimension, or ``None`` before the first embed."""
        return self._dimension

    def _load(self) -> Any:
        """Return the shared backend model, loading it once per name.

        Also disables the ``huggingface_hub`` Windows symlink warning
        (via ``HF_HUB_DISABLE_SYMLINKS_WARNING``, only when the user has
        not set it): the degraded cache layout it warns about is
        harmless for local inference, and the warning would otherwise
        fire on every first model download.
        """
        # Set before importing fastembed so the hub observes it.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        with _model_cache_lock:
            model = _model_cache.get(self._model_name)
            if model is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:
                    raise SemanticError(
                        "Semantic caching requires the optional semantic "
                        "dependencies. Install with "
                        'pip install "llmcachex[semantic]".'
                    ) from exc
                try:
                    model = TextEmbedding(self._model_name)
                except Exception as exc:
                    raise SemanticError(
                        f"Could not load local embedding model "
                        f"{self._model_name!r}: {exc}"
                    ) from exc
                _model_cache[self._model_name] = model
            return model

    def embed(self, text: str) -> list[float]:
        """Embed ``text`` locally and return the float vector.

        Raises:
            SemanticError: Invalid input, missing dependency, or model
                load failure.
        """
        if not isinstance(text, str) or not text.strip():
            raise SemanticError("Cannot embed an empty string.")
        model = self._load()
        try:
            vectors: Sequence[Sequence[float]] = list(model.embed([text]))
        except SemanticError:
            raise
        except Exception as exc:
            raise SemanticError(
                f"Local embedding failed for model {self._model_name!r}: {exc}"
            ) from exc
        if not vectors:
            raise SemanticError(
                f"Local embedding model {self._model_name!r} "
                "returned no vector."
            )
        vector = [float(value) for value in vectors[0]]
        if not vector:
            raise SemanticError(
                f"Local embedding model {self._model_name!r} "
                "returned an empty vector."
            )
        self._dimension = len(vector)
        return vector
