"""Opt-in semantic caching (local embeddings, no API keys).

Exact deterministic caching stays the default. When a decorated function
opts in with ``semantic=True``, an exact miss falls back to a similarity
search over previous entries: compatible candidates (same function,
provider, model, embedding model and semantic fields) ranked by cosine
similarity must clear a configurable threshold before their cached
response is reused.

Typical usage::

    from llmcachex import cached_call

    @cached_call(semantic=True, semantic_fields=["prompt"])
    def generate(prompt):
        return external_call(prompt)
"""

from .embeddings import (
    DEFAULT_SEMANTIC_DIMENSION,
    DEFAULT_SEMANTIC_MODEL,
    ENV_SEMANTIC_MODEL,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    SemanticError,
    resolve_model_name,
)
from .similarity import cosine_similarity
from .store import (
    DEFAULT_MAX_SEMANTIC_ENTRIES,
    SemanticCandidate,
    SemanticRecord,
    SemanticStore,
    pack_embedding,
    unpack_embedding,
    utc_now_iso,
)

__all__ = [
    "DEFAULT_MAX_SEMANTIC_ENTRIES",
    "DEFAULT_SEMANTIC_DIMENSION",
    "DEFAULT_SEMANTIC_MODEL",
    "ENV_SEMANTIC_MODEL",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "SemanticCandidate",
    "SemanticError",
    "SemanticRecord",
    "SemanticStore",
    "cosine_similarity",
    "pack_embedding",
    "resolve_model_name",
    "unpack_embedding",
    "utc_now_iso",
]
