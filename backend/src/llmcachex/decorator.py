"""Developer-facing caching decorator for LLMCacheX.

This module coordinates the building blocks: function invocations are
captured, hashed with :func:`llmcachex.hashing.hash_request`, results are
persisted with the configured cache backend (SQLite by default, Redis via
``strategy="redis"``, optionally with a TTL), and cache misses flow
through an optional process-local rate limiter
and retry-with-backoff before execution.

Execution order on each call:

1. Normalize arguments and generate the deterministic cache key.
2. Check the cache; a valid entry is returned immediately without
   consuming rate-limit capacity.
3. On a miss (or expired entry), acquire the rate limiter if configured.
4. Execute the function with retry logic.
5. Save the successful result (with TTL, if configured) and return it.

Every step updates the lightweight metrics recorder
(:mod:`llmcachex.metrics`) and the analytics event store
(:mod:`llmcachex.analytics`), which persist into the same SQLite cache file
so the local dashboard API can read real runtime state.

Provider/usage/cost metadata (optional ``provider``, ``model``,
``usage`` and ``pricing`` arguments) labels analytics events — and the
provider/model identity additionally joins the cache key so different
providers/models never share entries. This module never performs network
calls and needs no API key.

No provider-specific logic lives here beyond that metadata recording.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from os import PathLike
from time import perf_counter
from typing import Any, ParamSpec, TypeVar, cast, overload

from .analytics import (
    AnalyticsStore,
    EventType,
    HitType,
    PricingConfig,
    RequestEvent,
    Usage,
    coerce_usage,
    estimate_cost,
)
from .hashing import hash_request
from .metrics import MetricsRecorder
from .providers.base import ProviderAdapter
from .resilience import (
    RateLimiter,
    run_with_retry,
    validate_rate_limit_config,
    validate_retry_config,
)
from .semantic import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    SemanticError,
    SemanticRecord,
    SemanticStore,
    utc_now_iso as semantic_now_iso,
)
from .storage import (
    DEFAULT_KEY_PREFIX,
    CacheStorage,
    create_storage,
)

_DEFAULT_CACHE_PATH = "llmcachex.db"
_DEFAULT_BACKOFF_FACTOR = 0.5
_DEFAULT_RATE_PERIOD = 60.0
_DEFAULT_SEMANTIC_THRESHOLD = 0.92
_ENV_SEMANTIC_THRESHOLD = "SEMANTIC_THRESHOLD"

P = ParamSpec("P")
T = TypeVar("T")

_logger = logging.getLogger(__name__)


def _best_effort(action: Callable[[], Any], what: str, default: Any = None) -> Any:
    """Run one metrics/analytics bookkeeping action, tolerating storage failure.

    Bookkeeping must never break a working cached call: SQLite write
    failures (locked database, disk full, dropped table) and corrupt
    stored values are logged as a warning and degrade to ``default``.
    Programming errors (e.g. an invalid event construction) still
    propagate so they surface in tests rather than being hidden.
    """
    try:
        return action()
    except (sqlite3.Error, ArithmeticError):
        _logger.warning(
            "LLMCacheX: %s failed; continuing without it", what, exc_info=True
        )
        return default


def _build_request_data(
    func: Callable[P, T],
    bound_arguments: dict[str, object],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    """Build the deterministic request representation for a function call.

    Function identity (module + qualified name) is included so that two
    different functions called with identical arguments never share a
    cache entry. When a provider/model is configured, its identity is
    included too, so e.g. two Gemini models never share cached responses.

    Args:
        func: The wrapped function.
        bound_arguments: Arguments bound to ``func``'s signature, including
            defaults.
        provider: Provider label (``None`` when unconfigured).
        model: Model label (``None`` when unconfigured).

    Returns:
        A structured mapping suitable for :func:`hash_request`.
    """
    data: dict[str, object] = {
        "function": {
            "module": func.__module__,
            "qualname": func.__qualname__,
        },
        "arguments": bound_arguments,
    }
    # Omitted entirely when unconfigured, so existing cache entries of
    # provider-free functions keep byte-identical request data (and hashes).
    if provider is not None or model is not None:
        data["provider"] = {"name": provider, "model": model}
    return data


def _validate_semantic_threshold(threshold: float | None) -> float:
    """Validate the semantic similarity threshold (``0 <= t <= 1``).

    ``None`` resolves to the ``SEMANTIC_THRESHOLD`` environment variable
    when set, else the conservative default. This only supplies a
    default value — semantic caching itself still requires the explicit
    per-decorator opt-in (``semantic=True``).
    """
    if threshold is None:
        raw = os.environ.get(_ENV_SEMANTIC_THRESHOLD, "").strip()
        if not raw:
            return _DEFAULT_SEMANTIC_THRESHOLD
        try:
            threshold = float(raw)
        except ValueError:
            raise ValueError(
                "SEMANTIC_THRESHOLD must be a number between 0 and 1, "
                f"got {raw!r}."
            ) from None
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0.0 <= threshold <= 1.0
    ):
        raise ValueError(
            "semantic_threshold must be a number between 0 and 1, "
            f"got {threshold!r}."
        )
    return float(threshold)


def _validate_semantic_fields(
    fields: Sequence[str] | None,
) -> tuple[str, ...] | None:
    """Validate ``semantic_fields`` (names of bound arguments to embed)."""
    if fields is None:
        return None
    if (
        isinstance(fields, str)
        or not isinstance(fields, Sequence)
        or not fields
    ):
        raise ValueError(
            "semantic_fields must be a non-empty sequence of argument "
            f"names, got {fields!r}."
        )
    cleaned: list[str] = []
    for name in fields:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                "semantic_fields must contain non-empty argument names, "
                f"got {name!r}."
            )
        cleaned.append(name)
    return tuple(cleaned)


def _semantic_text(
    arguments: dict[str, object], fields: tuple[str, ...] | None
) -> str:
    """Build the canonical text embedded for semantic matching.

    With ``fields`` only those bound arguments are embedded; otherwise
    every bound argument is included (nothing is silently ignored).
    Values that are not JSON-serializable fall back to ``str()``.
    """
    if fields is not None:
        selected = {
            name: arguments[name] for name in fields if name in arguments
        }
    else:
        selected = dict(arguments)
    try:
        return json.dumps(
            selected, sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        return str(selected)


def _require_semantic_backend() -> None:
    """Fail fast when semantic caching is enabled but uninstallable."""
    if importlib.util.find_spec("fastembed") is None:
        raise SemanticError(
            "Semantic caching requires the optional semantic "
            "dependencies. Install with "
            'pip install "llmcachex[semantic]".'
        )


def _resolve_ttl_seconds(
    ttl_hours: float | None, ttl_seconds: float | None
) -> float | None:
    """Resolve TTL options to a number of seconds.

    Args:
        ttl_hours: Time-to-live in hours, or ``None``.
        ttl_seconds: Time-to-live in seconds, or ``None``.

    Returns:
        TTL in seconds, or ``None`` when no TTL was supplied (entries
        never expire).

    Raises:
        ValueError: If both options are supplied, or a supplied value is
            not a positive number.
    """
    if ttl_hours is not None and ttl_seconds is not None:
        raise ValueError(
            "Supply only one of ttl_hours or ttl_seconds, not both."
        )
    for name, value in (("ttl_hours", ttl_hours), ("ttl_seconds", ttl_seconds)):
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise ValueError(
                f"{name} must be a positive number of "
                f"{'hours' if name == 'ttl_hours' else 'seconds'}, "
                f"got {value!r}."
            )
    if ttl_hours is not None:
        return float(ttl_hours) * 3600.0
    if ttl_seconds is not None:
        return float(ttl_seconds)
    return None


@overload
def cached_call(_func: Callable[P, T]) -> Callable[P, T]: ...


@overload
def cached_call(
    _func: None = ...,
    *,
    strategy: str = ...,
    cache_path: str | PathLike[str] = ...,
    ttl_hours: float | None = ...,
    ttl_seconds: float | None = ...,
    retries: int = ...,
    backoff_factor: float = ...,
    rate_limit: int | None = ...,
    rate_period: float = ...,
    redis_url: str | None = ...,
    redis_prefix: str | None = ...,
    semantic: bool = ...,
    semantic_threshold: float | None = ...,
    semantic_fields: Sequence[str] | None = ...,
    semantic_model: str | None = ...,
    semantic_provider: EmbeddingProvider | None = ...,
    provider: str | ProviderAdapter | None = ...,
    model: str | None = ...,
    usage: Callable[..., Any] | None = ...,
    pricing: PricingConfig | None = ...,
) -> Callable[[Callable[P, T]], Callable[P, T]]: ...


def cached_call(
    _func: Callable[P, T] | None = None,
    *,
    strategy: str = "sqlite",
    cache_path: str | PathLike[str] = _DEFAULT_CACHE_PATH,
    ttl_hours: float | None = None,
    ttl_seconds: float | None = None,
    retries: int = 0,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    rate_limit: int | None = None,
    rate_period: float = _DEFAULT_RATE_PERIOD,
    redis_url: str | None = None,
    redis_prefix: str | None = None,
    semantic: bool = False,
    semantic_threshold: float | None = None,
    semantic_fields: Sequence[str] | None = None,
    semantic_model: str | None = None,
    semantic_provider: EmbeddingProvider | None = None,
    provider: str | ProviderAdapter | None = None,
    model: str | None = None,
    usage: Callable[..., Any] | None = None,
    pricing: PricingConfig | None = None,
) -> Callable[P, T] | Callable[[Callable[P, T]], Callable[P, T]]:
    """Cache a function's return value transparently.

    May be used bare (``@cached_call``) or with options. On a cache miss
    (or expired entry) the function executes through the optional rate
    limiter and retry logic; its JSON-compatible return value is stored
    and returned. On a cache hit the stored value is returned without
    executing the function and without consuming rate-limit capacity.
    Exceptions are never cached and propagate unchanged.

    Args:
        _func: The function being decorated (bare-decorator usage only).
        strategy: Cache backend to use: ``"sqlite"`` (default, local
            file) or ``"redis"`` (requires the optional ``redis``
            package and a running server). Unknown strategies raise
            ``ValueError`` — there is no silent fallback.
        cache_path: Filesystem path of the SQLite database file. Used as
            the cache for ``strategy="sqlite"``; under
            ``strategy="redis"`` it remains the local file for metrics
            and analytics counters/events.
        ttl_hours: Entry time-to-live in hours (mutually exclusive with
            ``ttl_seconds``). ``None`` (default) means entries never expire.
        ttl_seconds: Entry time-to-live in seconds (mutually exclusive with
            ``ttl_hours``). ``None`` (default) means entries never expire.
        retries: Retries after the initial attempt (total attempts equal
            ``retries + 1``). ``0`` (default) means no retry.
        backoff_factor: Base delay in seconds for exponential backoff
            between attempts (``backoff_factor * 2**n``).
        rate_limit: Maximum external executions per ``rate_period``.
            ``None`` (default) disables rate limiting. Cache hits never
            consume capacity.
        rate_period: Rate-limit window in seconds (default 60).
        redis_url: Redis server URL for ``strategy="redis"``, e.g.
            ``"redis://localhost:6379/0"``. Defaults to the local
            development URL. Ignored for ``"sqlite"``. Credentials in
            the URL are never logged or exposed.
        redis_prefix: Redis key namespace for ``strategy="redis"``
            (default ``"llmcachex:cache:"``). Clearing only removes keys
            inside this namespace. Ignored for ``"sqlite"``.
        semantic: Enable opt-in semantic caching (default ``False``).
            On an exact miss, the request's semantic text is embedded
            locally and the semantic index is searched; a compatible
            prior entry at or above ``semantic_threshold`` is returned
            without executing the function. Requires the optional
            ``semantic`` dependencies unless ``semantic_provider`` is
            given. ``False`` (default) preserves pure exact caching.
        semantic_threshold: Minimum cosine similarity (``0``–``1``;
            default ``0.92``, or ``SEMANTIC_THRESHOLD`` when set) for
            reusing a prior response. Higher is stricter; lower matches
            more but risks incorrect reuse.
        semantic_fields: Bound argument names embedded for matching
            (e.g. ``["prompt"]``). ``None`` (default) embeds every bound
            argument — specify fields to keep non-semantic configuration
            (such as temperature) from being merged.
        semantic_model: Local embedding model name (default follows
            ``SEMANTIC_MODEL``, else ``"BAAI/bge-small-en-v1.5"``).
            Changing it invalidates prior semantic rows automatically.
        semantic_provider: Custom :class:`~llmcachex.semantic.
            EmbeddingProvider` (advanced use and tests). When given, the
            ``semantic`` dependencies are not required.
        provider: Optional provider identity: a provider name
            (``"local"``, ``"gemini"``, ...) or a
            :class:`~llmcachex.providers.ProviderAdapter`. It labels
            analytics events and — together with ``model`` — becomes part
            of the cache key, so different providers/models never share
            entries. It is never used to make network calls and never
            carries secrets; ``None`` (default) records unknown and keeps
            the historical cache key.
        model: Optional model label. It labels analytics and joins the
            cache key when a provider is configured. When an adapter is
            supplied and this is ``None``, the adapter's model name is
            used.
        usage: Optional callable mapping the function's *return value*
            to provider-reported usage (``Usage``, a mapping with
            ``input_tokens``/``output_tokens``/``total_tokens``, or
            ``None``). Tokens are only ever recorded from this
            metadata — never estimated from text.
        pricing: Optional :class:`~llmcachex.analytics.PricingConfig`
            used to estimate cost when usage is known. No pricing is
            bundled: without configuration the estimated cost stays
            ``None`` ("unknown").

    Returns:
        The wrapped function, or a decorator when options are supplied.

    Raises:
        ValueError: If ``strategy`` is unknown, TTL/retry/rate-limit
            options are invalid, both TTL options are given, the
            semantic threshold/fields are invalid, or a semantic field
            is not a function argument.
        SemanticError: If ``semantic=True`` but the optional semantic
            dependencies are missing (and no ``semantic_provider`` was
            given).
        RedisStorageError: If ``strategy="redis"`` but the ``redis``
            package is missing or the server is unreachable. No silent
            fallback to SQLite ever happens.
    """
    if strategy not in ("sqlite", "redis"):
        raise ValueError(
            f"Unsupported cache strategy {strategy!r}. Supported "
            f"strategies: 'sqlite', 'redis'."
        )
    if not isinstance(semantic, bool):
        raise ValueError(
            f"semantic must be True or False, got {semantic!r}."
        )
    threshold = _validate_semantic_threshold(semantic_threshold)
    fields = _validate_semantic_fields(semantic_fields)
    if semantic and semantic_provider is None:
        _require_semantic_backend()
    ttl = _resolve_ttl_seconds(ttl_hours, ttl_seconds)
    validate_retry_config(retries, backoff_factor)
    validate_rate_limit_config(rate_limit, rate_period)

    # Optional analytics metadata (no network activity, no API keys):
    adapter: ProviderAdapter | None = None
    provider_label: str | None = None
    if isinstance(provider, str):
        if not provider.strip():
            raise ValueError(
                "provider must be a non-empty string when given as text."
            )
        provider_label = provider.strip()
    elif provider is not None:
        if not isinstance(provider, ProviderAdapter):
            raise ValueError(
                "provider must be a provider name (str) or a "
                "ProviderAdapter instance; "
                f"got {type(provider).__name__}."
            )
        adapter = provider

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        signature = inspect.signature(func)
        if fields is not None:
            unknown = [
                name for name in fields if name not in signature.parameters
            ]
            if unknown:
                raise ValueError(
                    "semantic_fields must name function arguments; "
                    f"unknown: {', '.join(unknown)}."
                )
        state_lock = threading.Lock()
        storage: CacheStorage | None = None
        metrics: MetricsRecorder | None = None
        analytics: AnalyticsStore | None = None
        semantic_store: SemanticStore | None = None
        embedder: EmbeddingProvider | None = (
            semantic_provider
            if semantic_provider is not None
            else (LocalEmbeddingProvider(semantic_model) if semantic else None)
        )
        # Compatibility namespace for semantic rows: backend identity plus
        # the location user data lives under (never secrets — the Redis
        # URL is represented by its key prefix only).
        normalized_prefix = redis_prefix or DEFAULT_KEY_PREFIX
        if not normalized_prefix.endswith(":"):
            normalized_prefix += ":"
        if strategy == "redis":
            semantic_namespace = f"redis:{normalized_prefix}"
        else:
            semantic_namespace = f"sqlite:{cache_path}"
        fields_signature = (
            ",".join(sorted(fields)) if fields is not None else "*all*"
        )
        limiter: RateLimiter | None = (
            RateLimiter(rate_limit, rate_period)
            if rate_limit is not None
            else None
        )
        function_label = f"{func.__module__}.{func.__qualname__}"

        def provider_name() -> str | None:
            """Static provider label (adapter-aware), never guessed."""
            if provider_label is not None:
                return provider_label
            if adapter is not None:
                return adapter.get_provider_name()
            return None

        def model_name() -> str | None:
            """Explicit model label, else the adapter's model."""
            if model is not None:
                return model
            if adapter is not None:
                return adapter.get_model_name()
            return None

        def usage_and_cost(result: object) -> tuple[Usage | None, Decimal | None]:
            """Extract provider-reported usage + estimated cost.

            Extraction/pricing failures degrade to "unknown" (``None``)
            — analytics must never break a working cached call, and
            tokens are never estimated.
            """
            extracted: Usage | None = None
            try:
                if usage is not None:
                    extracted = coerce_usage(usage(result))
                elif adapter is not None:
                    extracted = adapter.extract_usage(result)
            except Exception:
                extracted = None
            if extracted is None:
                return None, None
            try:
                if pricing is not None:
                    return extracted, estimate_cost(extracted, pricing)
                if adapter is not None:
                    return extracted, adapter.estimate_cost(extracted)
            except Exception:
                pass
            return extracted, None

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            nonlocal storage, metrics, analytics, semantic_store
            if storage is None:
                with state_lock:
                    if storage is None:
                        storage = create_storage(
                            strategy,
                            cache_path=cache_path,
                            redis_url=redis_url,
                            redis_prefix=redis_prefix,
                        )
                        metrics = MetricsRecorder(cache_path)
                        analytics = AnalyticsStore(cache_path)
                        if semantic:
                            semantic_store = SemanticStore(cache_path)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            request_hash = hash_request(
                _build_request_data(
                    func,
                    dict(bound.arguments),
                    provider=provider_name(),
                    model=model_name(),
                )
            )
            assert storage is not None
            assert metrics is not None
            assert analytics is not None
            # Single atomic read: entry validity (including TTL) is checked
            # exactly once, so an entry cannot be observed as "exists" and
            # then lost (returning None to the caller) between two queries.
            call_start = perf_counter()
            entry = storage.get_entry(request_hash)
            if entry is not None:
                lookup_ms = (perf_counter() - call_start) * 1000.0
                _best_effort(
                    lambda: metrics.record_cache_hit(function_label, lookup_ms),
                    "cache-hit metric",
                )
                # Attribute the hit to previous executions when known;
                # savings stay None unless a prior cost actually exists.
                prior = _best_effort(
                    lambda: analytics.resolve_prior_context(request_hash),
                    "prior-execution lookup",
                )
                _best_effort(
                    lambda: analytics.record(
                        RequestEvent(
                            event_type=EventType.CACHE_HIT,
                            function_name=function_label,
                            request_hash=request_hash,
                            cache_status="hit",
                            latency_ms=lookup_ms,
                            retry_count=0,
                            success=True,
                            provider=(
                                provider_name()
                                if provider_label is not None
                                or adapter is not None
                                else (prior.provider if prior else None)
                            ),
                            model=(
                                model_name()
                                if model is not None or adapter is not None
                                else (prior.model if prior else None)
                            ),
                            estimated_cost_saved=(
                                prior.estimated_cost if prior else None
                            ),
                            hit_type=HitType.EXACT,
                        )
                    ),
                    "cache-hit event",
                )
                return cast(T, entry.response)
            semantic_vector: list[float] | None = None
            semantic_model_name: str | None = None
            if semantic:
                # Exact miss with semantic opt-in: embed locally, search
                # compatible prior entries, and reuse the first one whose
                # exact entry is still valid. Hits bypass rate limiting
                # and retry exactly like exact hits.
                assert semantic_store is not None
                assert embedder is not None
                semantic_call_start = perf_counter()
                candidates: list = []
                try:
                    semantic_vector = embedder.embed(
                        _semantic_text(dict(bound.arguments), fields)
                    )
                    semantic_model_name = embedder.model_name()
                    candidates = semantic_store.find(
                        semantic_vector,
                        namespace=semantic_namespace,
                        function_name=function_label,
                        provider=provider_name(),
                        model=model_name(),
                        embedding_model=semantic_model_name,
                        dimension=len(semantic_vector),
                        fields_signature=fields_signature,
                        threshold=threshold,
                    )
                except SemanticError:
                    # Configuration/backend failures stay loud and
                    # actionable (missing dependency, unloadable model).
                    raise
                except Exception:
                    _logger.warning(
                        "LLMCacheX: semantic lookup failed; "
                        "executing instead",
                        exc_info=True,
                    )
                    candidates = []
                for candidate in candidates:
                    target = storage.get_entry(candidate.cache_key)
                    if target is None:
                        # Underlying entry expired or deleted: drop the
                        # stale semantic row and try the next candidate.
                        _best_effort(
                            lambda: semantic_store.remove(candidate.cache_key),
                            "semantic cleanup",
                        )
                        continue
                    semantic_ms = (
                        perf_counter() - semantic_call_start
                    ) * 1000.0
                    _best_effort(
                        lambda: metrics.record_semantic_hit(
                            function_label, semantic_ms
                        ),
                        "semantic-hit metric",
                    )
                    matched_prior = _best_effort(
                        lambda: analytics.resolve_prior_context(
                            candidate.cache_key
                        ),
                        "prior-execution lookup",
                    )
                    _best_effort(
                        lambda: analytics.record(
                            RequestEvent(
                                event_type=EventType.CACHE_HIT,
                                function_name=function_label,
                                request_hash=request_hash,
                                cache_status="hit",
                                latency_ms=semantic_ms,
                                retry_count=0,
                                success=True,
                                provider=(
                                    provider_name()
                                    if provider_label is not None
                                    or adapter is not None
                                    else (
                                        matched_prior.provider
                                        if matched_prior
                                        else None
                                    )
                                ),
                                model=(
                                    model_name()
                                    if model is not None
                                    or adapter is not None
                                    else (
                                        matched_prior.model
                                        if matched_prior
                                        else None
                                    )
                                ),
                                estimated_cost_saved=(
                                    matched_prior.estimated_cost
                                    if matched_prior
                                    else None
                                ),
                                hit_type=HitType.SEMANTIC,
                            )
                        ),
                        "semantic-hit event",
                    )
                    return cast(T, target.response)
            _best_effort(
                lambda: metrics.record_cache_miss(function_label),
                "cache-miss metric",
            )
            retries_seen = 0

            def on_retry(_exc: BaseException) -> None:
                nonlocal retries_seen
                retries_seen += 1
                _best_effort(
                    lambda: metrics.record_retry(function_label),
                    "retry metric",
                )
                _best_effort(
                    lambda: analytics.record(
                        RequestEvent(
                            event_type=EventType.RETRY,
                            function_name=function_label,
                            request_hash=request_hash,
                        )
                    ),
                    "retry event",
                )

            if limiter is not None and limiter.acquire():
                _best_effort(
                    lambda: metrics.record_rate_limit_wait(function_label),
                    "rate-limit metric",
                )
                _best_effort(
                    lambda: analytics.record(
                        RequestEvent(
                            event_type=EventType.RATE_LIMIT_WAIT,
                            function_name=function_label,
                            request_hash=request_hash,
                            cache_status="miss",
                        )
                    ),
                    "rate-limit-wait event",
                )
            execution_seconds = 0.0

            def timed_execution() -> T:
                # Accumulates actual attempt time across retries, excluding
                # backoff sleeps: this is true execution latency.
                nonlocal execution_seconds
                attempt_start = perf_counter()
                try:
                    return func(*args, **kwargs)
                finally:
                    execution_seconds += perf_counter() - attempt_start

            try:
                result = run_with_retry(
                    timed_execution,
                    retries=retries,
                    backoff_factor=backoff_factor,
                    on_retry=on_retry,
                )
            except BaseException:
                _best_effort(
                    lambda: metrics.record_failure(function_label),
                    "failure metric",
                )
                _best_effort(
                    lambda: analytics.record(
                        RequestEvent(
                            event_type=EventType.CACHE_MISS,
                            function_name=function_label,
                            request_hash=request_hash,
                            cache_status="miss",
                            latency_ms=execution_seconds * 1000.0,
                            retry_count=retries_seen,
                            success=False,
                            provider=provider_name(),
                            model=model_name(),
                        )
                    ),
                    "failed-request event",
                )
                raise
            _best_effort(
                lambda: metrics.record_execution(
                    function_label, execution_seconds * 1000.0
                ),
                "execution metric",
            )
            extracted, cost = usage_and_cost(result)
            _best_effort(
                lambda: analytics.record(
                    RequestEvent(
                        event_type=EventType.CACHE_MISS,
                        function_name=function_label,
                        request_hash=request_hash,
                        cache_status="miss",
                        latency_ms=execution_seconds * 1000.0,
                        retry_count=retries_seen,
                        success=True,
                        provider=provider_name(),
                        model=model_name(),
                        input_tokens=(
                            extracted.input_tokens if extracted else None
                        ),
                        output_tokens=(
                            extracted.output_tokens if extracted else None
                        ),
                        total_tokens=(
                            extracted.total if extracted else None
                        ),
                        request_units=(
                            extracted.request_units if extracted else None
                        ),
                        estimated_cost=cost,
                    )
                ),
                "cache-miss event",
            )
            expires_at: datetime | None = None
            if ttl is not None:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=ttl
                )
            try:
                storage.set(request_hash, result, expires_at=expires_at)
            except TypeError as exc:
                raise TypeError(
                    f"Return value of function {func.__qualname__!r} is not "
                    f"JSON serializable and therefore cannot be cached: {exc}"
                ) from exc
            if (
                semantic
                and semantic_vector is not None
                and semantic_model_name is not None
            ):
                # Index the fresh entry for future semantic matches. The
                # query vector is reused — no second embedding. Failures
                # here must not break an already-cached successful call.
                assert semantic_store is not None
                _best_effort(
                    lambda: semantic_store.add(
                        SemanticRecord(
                            cache_key=request_hash,
                            embedding=tuple(semantic_vector),
                            embedding_model=semantic_model_name,
                            dimension=len(semantic_vector),
                            namespace=semantic_namespace,
                            function_name=function_label,
                            provider=provider_name(),
                            model=model_name(),
                            fields_signature=fields_signature,
                            created_at=semantic_now_iso(),
                            expires_at=(
                                expires_at.isoformat()
                                if expires_at is not None
                                else None
                            ),
                        )
                    ),
                    "semantic index add",
                )
            return result

        return wrapper

    if _func is not None:
        return decorator(_func)
    return decorator
