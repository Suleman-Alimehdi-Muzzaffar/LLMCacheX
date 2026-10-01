"""Process-local rate limiting for LLMCacheX.

The limiter is intentionally simple: an in-process sliding window over
recent execution timestamps. It is not distributed and does not survive
process restarts.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_DEFAULT_RATE_PERIOD = 60.0


def validate_rate_limit_config(
    rate_limit: int | None, rate_period: float
) -> None:
    """Validate rate limiter configuration, raising ValueError when invalid.

    Args:
        rate_limit: Maximum executions per period, or ``None`` to disable.
        rate_period: Window length in seconds.

    Raises:
        ValueError: If ``rate_limit`` is not a positive int (or ``None``)
            or ``rate_period`` is not a positive number.
    """
    if rate_limit is None:
        pass
    elif isinstance(rate_limit, bool) or not isinstance(rate_limit, int):
        raise ValueError(
            f"rate_limit must be a positive integer or None, "
            f"got {rate_limit!r}."
        )
    elif rate_limit <= 0:
        raise ValueError(
            f"rate_limit must be > 0, got {rate_limit!r}."
        )
    if (
        isinstance(rate_period, bool)
        or not isinstance(rate_period, (int, float))
    ):
        raise ValueError(
            f"rate_period must be a positive number of seconds, "
            f"got {rate_period!r}."
        )
    if rate_period <= 0:
        raise ValueError(
            f"rate_period must be > 0, got {rate_period!r}."
        )


class RateLimiter:
    """Allow at most ``max_calls`` executions per ``period`` seconds.

    Uses a sliding window of recent execution timestamps and blocks with
    ``time.sleep()`` until capacity is available. Thread-safe for normal
    local use via an internal lock. Process-local only: not distributed.
    """

    def __init__(self, max_calls: int, period: float = _DEFAULT_RATE_PERIOD) -> None:
        """Create a rate limiter.

        Args:
            max_calls: Maximum executions allowed per window.
            period: Window length in seconds.

        Raises:
            ValueError: If the configuration is invalid.
        """
        validate_rate_limit_config(max_calls, period)
        assert isinstance(max_calls, int)
        self._max_calls = max_calls
        self._period = float(period)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Block until one execution slot is available, then consume it.

        Returns:
            ``True`` if execution had to wait for capacity, ``False`` if a
            slot was available immediately (used for rate-limit metrics).
        """
        waited = False
        while True:
            now = time.monotonic()
            with self._lock:
                cutoff = now - self._period
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return waited
                wait = self._calls[0] + self._period - now
            if wait > 0:
                waited = True
                time.sleep(wait)
