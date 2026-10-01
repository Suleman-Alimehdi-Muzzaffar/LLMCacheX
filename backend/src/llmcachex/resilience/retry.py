"""Synchronous retry with exponential backoff for LLMCacheX."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_DEFAULT_BACKOFF_FACTOR = 0.5


def validate_retry_config(retries: int, backoff_factor: float) -> None:
    """Validate retry configuration, raising ValueError when invalid.

    Args:
        retries: Number of retries after the initial attempt.
        backoff_factor: Base delay in seconds for exponential backoff.

    Raises:
        ValueError: If ``retries`` is not a non-negative int or
            ``backoff_factor`` is not a non-negative number.
    """
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise ValueError(
            f"retries must be a non-negative integer, got {retries!r}."
        )
    if retries < 0:
        raise ValueError(
            f"retries must be >= 0, got {retries!r}."
        )
    if (
        isinstance(backoff_factor, bool)
        or not isinstance(backoff_factor, (int, float))
    ):
        raise ValueError(
            "backoff_factor must be a non-negative number, got "
            f"{backoff_factor!r}."
        )
    if backoff_factor < 0:
        raise ValueError(
            f"backoff_factor must be >= 0, got {backoff_factor!r}."
        )


def run_with_retry(
    operation: Callable[[], T],
    *,
    retries: int = 0,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    on_retry: Callable[[BaseException], None] | None = None,
) -> T:
    """Execute ``operation`` with retries and exponential backoff.

    Total attempts equal ``retries + 1``. After a failed attempt ``n``
    (0-indexed, excluding the final attempt), execution sleeps for
    ``backoff_factor * (2 ** n)`` seconds. Failed results are never
    returned; if every attempt fails, the final exception propagates
    unchanged.

    Args:
        operation: Zero-argument callable performing the work.
        retries: Number of retries after the initial attempt.
        backoff_factor: Base delay in seconds for exponential backoff.
        on_retry: Optional observer invoked once per retry (before the
            backoff sleep) with the exception that caused it. Used by the
            metrics layer; it must not alter control flow.

    Returns:
        The first successful result of ``operation``.

    Raises:
        ValueError: If the retry configuration is invalid.
        Exception: The final exception raised by ``operation``.
    """
    validate_retry_config(retries, backoff_factor)
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= retries:
                raise
            if on_retry is not None:
                on_retry(exc)
            delay = backoff_factor * (2**attempt)
            if delay > 0:
                time.sleep(delay)
            attempt += 1
