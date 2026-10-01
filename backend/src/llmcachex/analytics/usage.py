"""Provider-supplied usage metadata.

Tokens are never estimated, counted or inferred from raw text here.
Values are only ever supplied by the caller (a provider adapter, an
extractor callback, or application code) — until real usage information
exists they stay ``None`` and are reported as "unavailable".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Usage"]


def _check_token(name: str, value: int | None) -> int | None:
    """Validate one token count: a non-negative int, or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int or None, got {value!r}.")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}.")
    return value


@dataclass(frozen=True)
class Usage:
    """Token/usage figures reported by a provider response.

    Every field is optional: ``None`` means "unknown / not supplied" and
    must never be presented as zero.

    Args:
        input_tokens: Prompt tokens consumed by the provider, or None.
        output_tokens: Completion tokens produced by the provider, or None.
        total_tokens: Total tokens as reported by the provider. When it is
            ``None`` but both ``input_tokens`` and ``output_tokens`` are
            known, :attr:`total` derives the exact sum (arithmetic on
            supplied values — never an estimate).
        request_units: Optional provider-specific billing units (for
            e.g. image or audio units), or None.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    request_units: int | None = None

    def __post_init__(self) -> None:
        _check_token("input_tokens", self.input_tokens)
        _check_token("output_tokens", self.output_tokens)
        _check_token("total_tokens", self.total_tokens)
        _check_token("request_units", self.request_units)

    @property
    def total(self) -> int | None:
        """Reported total, or the exact input+output sum when derivable."""
        if self.total_tokens is not None:
            return self.total_tokens
        if self.input_tokens is not None and self.output_tokens is not None:
            return self.input_tokens + self.output_tokens
        return None

    @property
    def has_token_data(self) -> bool:
        """True when at least one token figure is known."""
        return (
            self.input_tokens is not None
            or self.output_tokens is not None
            or self.total_tokens is not None
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Usage:
        """Build a :class:`Usage` from a provider-style mapping.

        Unknown keys are ignored; known keys must be non-negative ints
        or ``None``.
        """
        return cls(
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
            request_units=data.get("request_units"),
        )


def coerce_usage(value: Any) -> Usage | None:
    """Coerce an extractor result into a :class:`Usage`.

    Accepts ``None``, a :class:`Usage`, or a mapping of usage fields.
    Anything else raises ``TypeError`` (usage data must never be guessed).
    """
    if value is None:
        return None
    if isinstance(value, Usage):
        return value
    if isinstance(value, Mapping):
        return Usage.from_mapping(value)
    raise TypeError(
        f"Usage extractor must return Usage, a mapping, or None; "
        f"got {type(value).__name__}."
    )
