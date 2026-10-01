"""Deterministic request hashing for LLMCacheX.

This module converts structured Python data into a stable SHA-256 hash.
The same logical input always produces the same hash, regardless of
dictionary key ordering.
"""

from __future__ import annotations

import hashlib
import json
import math


def canonicalize(data: object) -> object:
    """Convert supported Python structures into a deterministic representation.

    Rules:
        - Dictionaries: keys must be strings, sorted consistently, with values
          canonicalized recursively.
        - Lists: order is preserved, elements canonicalized recursively.
        - Tuples: order is preserved and tagged distinctly from lists.
        - Primitives (str, int, float, bool, None): tagged explicitly so that
          values such as ``1`` and ``True`` never hash identically.

    Args:
        data: The Python object to canonicalize.

    Returns:
        A JSON-serializable tagged structure with deterministic ordering.

    Raises:
        TypeError: If ``data`` (or a nested value) is of an unsupported type,
            if a dictionary key is not a string, or if a float is non-finite.
    """
    if data is None:
        return ["none", None]
    if isinstance(data, bool):
        return ["bool", data]
    if isinstance(data, int):
        return ["int", data]
    if isinstance(data, float):
        if not math.isfinite(data):
            raise TypeError(
                f"Non-finite float values are not supported for deterministic "
                f"hashing: {data!r}."
            )
        return ["float", data]
    if isinstance(data, str):
        return ["str", data]
    if isinstance(data, dict):
        items: list[object] = []
        for key, value in data.items():
            if not isinstance(key, str):
                raise TypeError(
                    "Only dictionaries with string keys are supported for "
                    f"deterministic hashing, got key of type "
                    f"{type(key).__name__!r}."
                )
            items.append([key, canonicalize(value)])
        items.sort(key=lambda pair: pair[0])  # type: ignore[index]
        return ["dict", items]
    if isinstance(data, list):
        return ["list", [canonicalize(item) for item in data]]
    if isinstance(data, tuple):
        return ["tuple", [canonicalize(item) for item in data]]
    raise TypeError(
        f"Object of type {type(data).__name__!r} is not supported for "
        f"deterministic hashing. Supported types are dict, list, tuple, "
        f"str, int, float, bool and None."
    )


def hash_request(data: object) -> str:
    """Hash structured request data deterministically with SHA-256.

    Dictionary key ordering (including nested dictionaries) does not affect
    the resulting hash, while list/tuple order does. Values ``1`` and
    ``True`` produce different hashes.

    Args:
        data: Structured Python data (dicts, lists, tuples and primitives).

    Returns:
        Lowercase SHA-256 hexadecimal digest string.

    Raises:
        TypeError: If ``data`` contains unsupported object types.
    """
    canonical = canonicalize(data)
    serialized = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
