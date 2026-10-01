"""Cosine similarity for semantic cache matching (dependency-free).

Operates on plain float sequences so neither NumPy nor the embedding
backend leaks into the matching logic. Embeddings of different
dimensions are rejected; zero vectors yield ``0.0`` instead of a
division error. Results are plain floats in ``[-1.0, 1.0]``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = ["cosine_similarity"]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        ``dot(a, b) / (|a| * |b|)`` clamped to ``[-1.0, 1.0]``;
        ``0.0`` when either vector has zero magnitude.

    Raises:
        ValueError: If the vectors are empty or have different lengths.
    """
    if len(a) != len(b):
        raise ValueError(
            f"Cannot compare vectors of different lengths: "
            f"{len(a)} != {len(b)}."
        )
    if len(a) == 0:
        raise ValueError("Cannot compare empty vectors.")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        x = float(x)
        y = float(y)
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    denominator = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denominator == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / denominator))
