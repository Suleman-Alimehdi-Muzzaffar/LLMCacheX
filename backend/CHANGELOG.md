# Changelog

## [0.1.0] - Unreleased

### Added

- Initial package structure
- Python 3.13 development environment
- Modern pyproject.toml configuration
- src-based package layout
- Deterministic SHA-256 request hashing (`llmcachex.hashing`)
- SQLite-backed response cache (`llmcachex.storage.SQLiteStorage`)
- Developer-facing `@cached_call` decorator
- TTL support (`ttl_hours` / `ttl_seconds`) with `expires_at` column and
  automatic migration for pre-Phase-4 databases
- Retry with exponential backoff (`retries` / `backoff_factor`)
- Process-local sliding-window rate limiting (`rate_limit` / `rate_period`);
  cache hits bypass the limiter
- Configuration validation (`ValueError` on invalid TTL/retry/rate options)
- pytest suite (`backend/tests`: hasher, storage, decorator, TTL, retry,
  rate limiter, combined integration)

### Phase 5 (hardening)

#### Added

- `tests/test_edge_cases.py` — hashing edge cases, JSON type round trips,
  cache corruption, path handling, API surface, parameterized-SQL checks
- `tests/test_concurrency.py` — multi-threaded storage, decorator and
  rate-limiter validation with documented limitations
- Additional coverage: default/mixed argument normalization, persistence
  across reopen, retry attempt counts (0/1/2/3), TTL+retry, TTL+rate-limit
  and retry+rate-limit combinations, expired entries passing through the
  rate limiter

#### Fixed

- `SQLiteStorage` shared across threads now works: connections were created
  with `check_same_thread=True`, raising `ProgrammingError` for any
  multi-threaded usage; operations are now serialized with an internal
  re-entrant lock (still single-process only)
- `SQLiteStorage` now creates missing parent directories of the database
  path (previously `sqlite3.OperationalError: unable to open database file`
  for nested cache paths)
- `@cached_call` now reads the cache with a single `get_entry()` call
  instead of `exists()` + `get()`; an entry expiring between the two
  queries could previously return `None` to the caller instead of
  executing the function

### Phase 6 (dashboard API + frontend)

#### Added

- `llmcachex.metrics` — persistent request metrics: hit/miss/execution
  counters, retry, rate-limit wait and failure counts, average/latest
  execution latency, and a bounded activity log (200 events). Counters
  and events are stored in the **same SQLite cache file**
  (`metrics_counters`, `activity_events` tables, WAL mode) so the API
  process reports the application's real activity without Redis or any
  external database
- Metrics integration in `@cached_call` (hits, misses, execution latency
  via `perf_counter`, retry attempts, rate-limit waits, failures)
- `SQLiteStorage.list_entries()` + `CacheEntryMeta` — cache entry
  metadata (no values) for the dashboard
- `run_with_retry(..., on_retry=...)` callback hook;
  `RateLimiter.acquire() -> bool` (also keeps the sleep behaviour used
  by the decorator)
- FastAPI dashboard API in `backend/api/` (separate from the core
  package, which still has zero runtime dependencies):
  `GET /api/health`, `GET /api/stats`, `GET /api/cache`,
  `GET /api/activity`, `DELETE /api/cache`, plus Pydantic response
  schemas, CORS for the local dev origins and clean `sqlite3.Error`
  → `503` handling. Cache DB path via `LLMCACHEX_CACHE_DB`
- Optional install extras: `pip install -e "./backend[api]"` (FastAPI,
  Uvicorn), `[dev]` adds pytest/httpx
- Tests: `tests/test_metrics.py`, `tests/test_api.py`, plus additions to
  storage/retry/rate-limiter tests
- React dashboard in `frontend/` wired to the live API: metric cards,
  cache table with Refresh/Clear, activity list, loading/error/retry
  states; API base URL via `VITE_API_BASE_URL` in `frontend/.env`
  (no hardcoded or fabricated statistics anywhere)

#### Fixed

- `latest_execution_latency_ms` was accumulated instead of replaced
  (gauge vs. delta counter handling in the metrics recorder)
- The dashboard API now opens its cache database at server startup
  (FastAPI lifespan) instead of at import time — importing `api.main`
  no longer creates `llmcachex.db` as a side effect

### Phase 7 (analytics, usage, providers, cost)

#### Added

- `llmcachex.analytics.usage` — frozen `Usage` dataclass for
  provider-reported token counts (`input`, `output`, optional
  `cache_read`/`cache_write`/`request_units`) with validation and
  `total` derivation; `coerce_usage` accepts plain mappings or `Usage`
- `llmcachex.analytics.cost` — `PricingConfig`: configurable Decimal
  prices per 1k units (no hardcoded prices anywhere), `estimate()` that
  returns `None` when usage or pricing is unknown (never a fabricated
  `0`), `coerce_decimal` for safe `str | int | float | Decimal` parsing
- `llmcachex.analytics.events` — `EventType` vocabulary and the
  `RequestEvent` model (function, provider/model labels, timestamp,
  latency, success, outcome detail, tokens, `estimated_cost`,
  `estimated_cost_saved`, `request_units`); no raw prompts, arguments
  or responses are ever recorded
- `llmcachex.analytics.metrics` — `AnalyticsStore`: structured
  `analytics_events` table in the same SQLite cache file (indexed,
  Decimal costs stored as TEXT, Python-side exact aggregation),
  bounded retention (default 1000 events), `summary`/`timeseries`/
  `providers`/`usage_totals` aggregations, `resolve_prior_context`
  for cache-hit savings attribution
- `llmcachex.providers` — provider-independent abstraction:
  runtime-checkable `ProviderAdapter` protocol, `BaseProviderAdapter`
  defaults, non-network `StaticAdapter` (clearly marked as offline),
  and a thread-safe local registry (`register_provider`,
  `get_provider`, `unregister_provider`, `list_providers`,
  `UnknownProviderError`)
- `@cached_call` analytics parameters: `provider` (name, adapter or
  `ProviderAdapter`), `model`, `usage` (callable returning token
  counts) and `pricing` (`PricingConfig` or compatible mapping).
  Decorator now records `cache_hit`/`cache_miss`/`retry`/
  `rate_limit_wait` events with execution latency, success/failure and
  estimated cost; hits inherit provider/model context and record
  `estimated_cost_saved` from the prior miss (cost is never counted
  twice). Usage/pricing extraction errors degrade to `None` and never
  break a cached call. Provider/model are analytics labels only — not
  part of the cache key
- `MetricsRecorder` now accepts `max_events` (retention no longer
  hardcoded to 200)
- Analytics API: `GET /api/analytics/summary`,
  `GET /api/analytics/timeseries?bucket=hour|day&hours=1..8760`,
  `GET /api/analytics/providers`, `GET /api/analytics/usage`,
  `DELETE /api/analytics`, with Pydantic response schemas; summary is
  derived from analytics events (clearing analytics resets it; Phase 6
  counters and cache entries are untouched)
- Tests: `tests/test_analytics.py`, `tests/test_providers.py`,
  `tests/test_analytics_api.py` (199 tests passing in total after the
  Phase 9 audit)
- React analytics page at `#/analytics` (minimal hash routing, no
  router dependency): summary cards, hand-built SVG charts (requests
  with hourly/daily toggle, latency, cost, stacked hit/miss bar),
  per-provider table with "Unknown" badges, cost-insight panel with
  the "estimates, not billing" note, and Clear Analytics — all unknown
  values render as `N/A`, never `0`

### Phase 8 (packaging & release readiness)

#### Added

- Release builds: `python -m build` produces `llmcachex-0.1.0` sdist
  and wheel; verified contents (wheel ships only `llmcachex/`,
  `py.typed` and `dist-info` with LICENSE — no tests, frontend, env
  files, databases or caches)
- `build` and `ruff` added to the `[dev]` extra for release and
  quality workflows
- Lightweight lint configuration (`[tool.ruff]` in `pyproject.toml`:
  pyflakes + critical pycodestyle only) — passes clean over
  `src`, `api` and `tests`
- GitHub Actions CI workflow (`.github/workflows/ci.yml`): installs
  the package with the `dev` extra on Python 3.13 (Linux + Windows),
  runs ruff, runs the full pytest suite and verifies installation
  imports — **no PyPI publication, no deployment**
- `RELEASE_CHECKLIST.md` — the pre-release verification checklist
- README rewritten for release: problem statement, features,
  installation, quick start, cache/TTL/retry/rate-limit behavior,
  analytics, providers, dashboard, full API reference, configuration,
  security guidance, architecture diagram, limitations and roadmap

#### Changed

- Version is now single-sourced from `llmcachex.__version__`
  (`dynamic = ["version"]` in `pyproject.toml`) so metadata and the
  package can never disagree
- Modernized license metadata to PEP 639 SPDX form
  (`license = "MIT"`, `setuptools>=77`) and removed the deprecated
  license classifier; `Author` metadata and
  `Development Status :: 3 - Alpha` classifier added
- `py.typed` is now declared as package data (ships in the wheel)
- `.gitignore` extended for build/lint artifacts (`.ruff_cache/`,
  `*.egg`)
- Removed one stale `noqa` directive in `tests/test_edge_cases.py`

#### Not done (intentionally)

- **PyPI publication was NOT performed** (no PyPI token created; no
  publish step exists in CI)
- No provider integrations, no new runtime features, no
  authentication, no deployment

### Phase 9 (final architecture, security & release-readiness audit)

#### Fixed

- SQLite write paths (`SQLiteStorage.set`/`clear`,
  `MetricsRecorder._record`, `AnalyticsStore.record`/`clear`) now roll
  back on failure — a failed write can no longer leave an open
  transaction holding the database write lock and cascading
  `database is locked` errors into every other writer
- Metrics/analytics bookkeeping failures are now best-effort: a failed
  record or prior-context lookup logs a warning and degrades instead of
  breaking a successful cached call (previously an analytics failure
  could mask the original exception on the failure path)
- Dashboard API now returns the documented `503` (was `500`) with a
  clean body when the cache database is unreadable
- Misleading private constant `_MILLIS_PER_UNIT` renamed to
  `_TOKENS_PER_1K` (same value, same arithmetic)

#### Removed

- Unused `AnalyticsStore.resolve_prior_execution` (superseded by
  `resolve_prior_context`, which also attributes hits to prior misses
  without known cost)

#### Documentation

- README Limitations now document cache-key identity
  (`module.qualname` + arguments — closures/lambdas sharing a qualname
  can share cache entries) and best-effort analytics recording
- Stale `196 tests` claims updated to `199`; stale Phase 5 note in
  `tests/__init__.py` removed

#### Tests (199 passing in total)

- Broken-analytics degradation: hits/misses still return and cache with
  the analytics table dropped, a warning is logged, and the function's
  original exception is never replaced by a sqlite error
- API: unreadable cache table returns a clean `503` (no traceback, no
  filesystem paths)

### Phase 10 (Gemini provider integration)

#### Added

- `llmcachex.providers.gemini.GeminiProvider` — first real LLM provider
  adapter (official `google-genai` SDK only; legacy
  `google-generativeai` is not used). Handles client setup, `generate()`,
  response text extraction, SDK `usage_metadata` parsing and
  provider/model identity. No SQLite/cache logic inside the adapter
- Optional `gemini` extra (`pip install "llmcachex-core[gemini]"`); the core
  package keeps zero runtime dependencies and never requires the SDK
- Provider-aware cache identity: configured provider/model joins the
  request hash so different models never share entries (provider-free
  calls keep byte-identical historical keys; the API key is never part
  of the key and never stored)
- Configurable model: explicit argument, else `GEMINI_MODEL`, else
  `gemini-3.8-flash`
- Lazy client initialization: importing or constructing the provider
  never requires a key; a missing key raises `GeminiConfigurationError`
  with a clear message only when a request is attempted
- `GeminiProviderError` for API/network/model failures, with API-key
  redaction in messages
- `backend/.env.example` (`GEMINI_API_KEY` placeholder only, plus
  optional `GEMINI_MODEL`); `backend/.env` stays git-ignored
- README "Gemini provider" section (install, key/model config, caching,
  usage/cost, errors, dashboard)

#### Tests (229 passing in total)

- `tests/test_gemini_provider.py` (30 tests, fully mocked — no live
  quota used): init/model config, missing-key and missing-SDK behavior,
  metadata, usage extraction (incl. unknown → `None`), generation,
  error mapping with key redaction, cost math, provider-key separation,
  cache hit/miss, TTL expiry, retry, rate limiting, analytics
  (provider/model/tokens/cost/savings), stored-data secret scan
- Live Gemini verification performed once manually (miss → hit → TTL
  re-call) outside the automated suite

### Phase 11 (Gemini/provider audit & hardening)

#### Fixed

- `llmcachex.providers` no longer imports the Gemini adapter at package
  import time: Gemini names resolve lazily (PEP 562), so importing the
  core library or the provider package never requires `google-genai`
- README Gemini example now passes `provider=gemini` to `@cached_call`,
  matching the documented cache-identity and analytics behavior
- Recreated `backend/.env.example` (placeholders only)

#### Tests (235 passing in total)

- Retry analytics: fail-twice-then-succeed counts 1 request / 2 retries /
  1 success; total failure caches nothing and counts 1 failed request
- API keys with the same model share one cache entry (key never hashed)
- Gemini registry round-trip (register/get/case-insensitive/duplicate/
  unknown/unregister) and configured-model event labels
- Hermetic subprocess proof: core caching plus provider construction
  work with the `google` SDK blocked from import

### Phase 12 (Redis cache backend)

#### Added

- `llmcachex.storage.RedisStorage` — second cache backend (official
  `redis-py` client only). JSON documents (never pickle) under the
  `llmcachex:cache:` namespace, native `SET … EX` TTL plus stored
  expiry metadata, `SCAN`-based listing/clearing (no `KEYS *`, no
  `FLUSHDB`/`FLUSHALL`), connection pooling, fail-fast availability
  check with actionable credential-free errors
- Minimal `CacheStorage` protocol (`get_entry`/`get`/`set`/`exists`/
  `list_entries`/`clear`/`close`) satisfied structurally by both
  backends; `create_storage()` factory (`ValueError` on unknown
  strategy, never silent fallback)
- `strategy="redis"` on `@cached_call` (`redis_url`, `redis_prefix`
  options); SQLite remains the default and `cache_path` keeps serving
  metrics/analytics under Redis
- Optional `redis` extra (`pip install "llmcachex-core[redis]"`); core keeps
  zero required dependencies and imports without `redis-py`
- Dashboard API serves the configured backend (`CACHE_STRATEGY`,
  `REDIS_URL`, `REDIS_KEY_PREFIX`); `/api/health` reports
  `cache_backend` (never URLs/passwords); `RedisStorageError` maps to a
  clean `503`. Dashboard header shows `Cache: SQLite/Redis`
- `backend/.env.example` cache-backend placeholders; README "Redis
  cache backend" section (Docker, usage, TTL, namespaces, security,
  `pytest -m redis`)

#### Tests (272 passing in total)

- `tests/test_redis_storage.py` (offline fakes): protocol/factory,
  init validation, CRUD, TTL metadata, corruption, namespace isolation,
  decorator miss/hit/expiry, backend-independent hashes, API round-trip,
  clean `503`s, env config, source scan (no flush/keys/pickle)
- `tests/test_redis_integration.py` (`pytest -m redis`, live Docker
  server): real SET/GET/TTL/clear isolation, decorator + analytics,
  API over Redis, connection errors

### Phase 13 (opt-in semantic caching)

#### Added

- `llmcachex.semantic` package: dependency-free cosine similarity,
  `EmbeddingProvider` protocol, lazy thread-safe `LocalEmbeddingProvider`
  (local ONNX inference via optional `fastembed`, default
  `BAAI/bge-small-en-v1.5`), and a persistent bounded SQLite semantic
  index (linear scan over compatible candidates — documented, not a
  vector database)
- Opt-in decorator API: `semantic=`, `semantic_threshold=` (0–1,
  default 0.92, `SEMANTIC_THRESHOLD` fallback), `semantic_fields=`,
  `semantic_model=` (`SEMANTIC_MODEL` fallback),
  `semantic_provider=` (custom/test embedders). Pure exact caching
  remains the default; exact hash is always checked first
- Compatibility-scoped matching (function, provider, model, embedding
  model + dimension, fields, namespace) with lazy invalidation of stale
  rows; TTL/expiry enforced via the exact backend; `DELETE /api/cache`
  also clears semantic rows
- `semantic_hits` tracked in metrics counters, analytics summary,
  `/api/stats`, `/api/analytics/summary` and the dashboard (new
  Semantic Hits card + activity label); semantic hits reuse the
  existing avoided-cost savings logic
- Optional `semantic` extra (`pip install "llmcachex-core[semantic]"`);
  core keeps zero required dependencies; fail-fast install hint when
  enabled without the extra
- `backend/.env.example` semantic placeholders; README "Semantic
  caching" section (15 topics: behavior, fields, model, privacy, scale)

#### Tests (311 passing in total)

- `tests/test_semantic.py` (deterministic fakes, no downloads): cosine
  edge cases, provider laziness, validation, exact regression, hit/miss,
  precise threshold boundary, function/model/provider isolation, field
  scoping, TTL invalidation, clear, analytics counts, old-DB migration,
  API metrics without secrets, Redis strategy, rate-limit bypass
- `pytest -m semantic`: live check with the real local model (relative
  similarity properties only; skipped when unfetchable)

### Release preparation follow-up

#### Fixed

- Test-suite `StarletteDeprecationWarning` (`starlette.testclient`
  recommending `httpx2`): `httpx2>=2.0` added to the `dev` extra, so
  `pytest` runs warning-free
- `huggingface_hub` Windows symlink-cache notice: the semantic backend
  now sets `HF_HUB_DISABLE_SYMLINKS_WARNING=1` (only when unset by the
  user) before loading the local embedding model
