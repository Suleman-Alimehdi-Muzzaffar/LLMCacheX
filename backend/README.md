# LLMCacheX

> A lightweight, developer-focused Python library for caching and adding
> resilience to repeated LLM/API requests — with optional analytics,
> configurable cost estimation and a local dashboard.

## What is LLMCacheX?

LLMCacheX is a small, dependency-free Python library that wraps any
function — typically one that calls an external LLM or metered HTTP API —
with a single decorator. It caches responses in a local SQLite database,
adds retry with exponential backoff and process-local rate limiting, and
records request analytics (including configurable cost estimates) that a
bundled local dashboard can visualize.

The library is **provider-independent**: it contains no integrations with
any specific LLM provider, performs **no network calls** of its own, and
requires **no API key** to install, run or develop.

## The problem it solves

Calls to LLM providers are:

- **Slow** — every identical prompt pays full latency again.
- **Expensive** — every identical prompt pays full token cost again.
- **Fragile** — transient failures and rate limits interrupt workflows.

Application code usually grows ad-hoc caching, try/except retry loops and
sleep-based throttling, scattered across call sites. LLMCacheX makes this
optimization invisible: add a decorator to an existing function, keep
calling it the same way, and caching, retries, rate limiting and metrics
happen underneath — without rewriting the application.

## Key features

- **Deterministic request hashing** — SHA-256 over normalized arguments
  (dict-order independent, type-disambiguated).
- **SQLite response cache** — zero-config local storage, WAL mode,
  thread-safe, optional TTL expiration.
- **`@cached_call` decorator** — add caching/resilience without changing
  function bodies.
- **TTL expiration** — stale entries are never served; expired entries
  behave as cache misses.
- **Retry with exponential backoff** — configurable attempt count and
  backoff; failed results are never cached.
- **Process-local rate limiting** — sliding-window throttle; cache hits
  bypass it (that is where the cost is saved).
- **Request analytics** — structured events (hits, misses, retries,
  rate-limit waits) with bounded retention, all in the local SQLite file.
- **Usage & cost estimation** — optional token usage extraction and
  Decimal-based pricing you configure yourself; unknown values stay
  `None`/`N/A`, never a fabricated `0`.
- **Cache savings attribution** — estimated cost avoided by cache hits.
- **Provider abstraction** — labels and a registry for your own adapters;
  no provider is bundled.
- **Local dashboard API + React UI** — live stats, cache table, activity
  feed and an analytics page with charts (optional layers, never imported
  by the core library).

## Installation

```bash
pip install llmcachex-core            # core library (zero runtime dependencies)
pip install "llmcachex-core[api]"     # + FastAPI/Uvicorn for the dashboard API
pip install "llmcachex-core[dev]"     # + pytest, ruff, build tooling (contributors)
```

> **Distribution vs import name.** The PyPI distribution is
> `llmcachex-core`, but the Python package you import is `llmcachex`:
>
> ```python
> from llmcachex import cached_call
> ```

From a source checkout (editable, for development):

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./backend[dev]"
```

**No API key is required** — not for installation, not for the library,
not for the dashboard. LLMCacheX never talks to an LLM provider itself.
The optional Gemini adapter (`llmcachex-core[gemini]`) uses your own
provider credentials; other vendors (OpenAI, Anthropic, …) remain
*future, optional* functionality (see [Roadmap](#roadmap)).

Requires **Python 3.13+**. License: MIT.

## Quick start

```python
from llmcachex import cached_call

@cached_call(ttl_hours=24)
def generate(prompt):
    return some_external_call(prompt)   # any function you already have

result = generate("Hello")
```

What happens:

1. **First call** — the arguments are hashed, the cache is consulted and
   **misses**.
2. The function **executes** (`some_external_call`).
3. The result is **saved** to `llmcachex.db` (a local SQLite file,
   created on demand) with a 24-hour expiry.
4. **Repeated call** with the same arguments — a **hit**. The function is
   not executed again; the cached result is returned instantly.

Nothing is ever sent anywhere: the cache file lives wherever you run your
code. The example uses a placeholder function — no API key, no provider.

By default the cache file is `llmcachex.db` in the working directory;
override it with `cache_path=...` on the decorator.

## Cache behavior

- **Key** = deterministic SHA-256 hash of the function's arguments
  (positional/keyword/defaults normalized; dicts hashed independent of
  insertion order; types disambiguated so `1` and `"1"` never collide).
- **Hit** — a valid, non-expired entry exists: the cached value is
  returned immediately, without touching the rate limiter.
- **Miss** — no entry (or expired entry): the rate limiter (if
  configured) is consulted, the function runs with retry protection, and
  the successful result is stored.
- **Failures are never cached.** Only successful results are stored.
- **Storage** — SQLite (WAL mode) with an internal lock: safe across
  threads within one process (single-process design; see
  [Limitations](#limitations)).
- **No value is ever exposed** through the dashboard API — only entry
  metadata (hash, age, TTL, hit count).

## TTL

Entries never expire by default. Supply exactly one of:

```python
@cached_call(ttl_hours=24)      # or
@cached_call(ttl_seconds=90)
```

Expired entries are treated as cache misses — the stale value is never
returned. Both values must be positive; supplying both (or neither value
that is non-positive) raises `ValueError` at decoration time.

## Retry

```python
@cached_call(retries=3, backoff_factor=0.5)
def generate(prompt):
    ...
```

- `retries=3` → the initial attempt plus up to 3 retries (4 attempts
  total).
- Between attempts the decorator sleeps `backoff_factor * 2**n` seconds
  (0.5s, 1s, 2s, …). `backoff_factor=0` retries immediately.
- If all attempts fail, the **final exception propagates unchanged** and
  nothing is written to the cache.

## Rate limiting

```python
@cached_call(rate_limit=5, rate_period=60)
def generate(prompt):
    ...
```

- At most `rate_limit` external executions per `rate_period` seconds
  (default period: 60s), using an in-memory sliding window.
- Excess executions **wait** (`time.sleep`) until capacity frees up.
- **Cache hits return immediately and do not consume capacity** — hits
  are precisely what avoids LLM/API cost and rate-limit budget.
- The limiter is **process-local**: not distributed across processes or
  machines, not persisted across restarts.

## Analytics

Optionally annotate the decorator to record rich request events:

```python
from llmcachex import cached_call
from llmcachex.analytics import PricingConfig

@cached_call(
    provider="my-service",                     # any label you choose
    model="my-model",                          # any label you choose
    usage=lambda result: result.get("usage"),  # how to read token counts
    pricing=PricingConfig.from_values(input_per_1k="0.003",
                                      output_per_1k="0.006"),
)
def generate(prompt):
    ...
```

- **Events** — every call records a structured event (`cache_hit`,
  `cache_miss`, `retry`, `rate_limit_wait`) into a bounded
  `analytics_events` table in the same SQLite cache file (default
  retention: 1000 events, oldest trimmed first). Events carry the
  function name, labels, timestamp, latency and outcome —
  **never raw prompts, arguments or responses**.
- **Usage** — token counts come only from *your* `usage` function
  (provider-reported). LLMCacheX never estimates tokens with a
  tokenizer.
- **Cost** — `PricingConfig` holds *your configured* prices (Decimal,
  per 1k units). There are **no hardcoded prices** anywhere. If usage or
  pricing is missing, cost fields are `None` (displayed as `N/A`) —
  never `0`.
- **Savings** — cache hits record `estimated_cost_saved`, derived from
  the prior miss's pricing, so the dashboard can show what caching
  avoided. Cost is never counted twice.
- **Latency** — average latency is computed from cache-miss executions
  only (`None` until an execution occurs).
- **Failure isolation** — if usage/pricing extraction raises, the
  analytics fields degrade to `None`; the cached call itself is never
  broken by analytics.

## Provider abstraction

`llmcachex.providers` provides a small, explicit abstraction — it is a
*label and metadata* layer, not a client:

- `ProviderAdapter` — a runtime-checkable protocol (name, model,
  optional usage/pricing helpers).
- `BaseProviderAdapter` / `StaticAdapter` — offline implementations
  (`StaticAdapter` performs no I/O; clearly marked as such).
- Registry — `register_provider()`, `get_provider("name")`,
  `list_providers()`, `unregister_provider()` with
  `UnknownProviderError` for unknown names.

Provider and model are recorded as analytics labels; when a provider
is configured they additionally join the cache key, so different
providers/models never share entries (see [Limitations](#limitations)).
`GeminiProvider` is the first real (network) adapter — see
[Gemini provider](#gemini-provider). No other vendor adapter is bundled.

## Gemini provider

Google Gemini is the first real LLM provider, via the official
`google-genai` SDK. The adapter lives only in
`llmcachex.providers.gemini`; the core engine stays provider-independent.

```powershell
pip install "llmcachex-core[gemini]"
```

```powershell
# backend environment only — never in code, never in frontend variables
$env:GEMINI_API_KEY = "your_key_here"  # placeholder, not a real credential
# $env:GEMINI_MODEL = "gemini-3.8-flash"  # optional override
```

```python
from llmcachex import cached_call
from llmcachex.providers.gemini import GeminiProvider

gemini = GeminiProvider(model="gemini-3.8-flash")  # or omit: env/default

@cached_call(
    ttl_hours=24, retries=2, rate_limit=5, rate_period=60,
    provider=gemini,  # scopes cache identity + analytics to this provider
)
def generate(prompt):
    return gemini.generate(prompt)

generate("What is a cache?")  # miss: calls Gemini, caches the answer
generate("What is a cache?")  # hit: Gemini is NOT called again
```

What you get:

- **Cache identity** — the configured provider/model joins the cache
  key, so `gemini-3.8-flash` and another model never share entries.
  The API key is never part of the key and never stored.
- **Resilience** — TTL, retry/backoff and rate limiting apply to Gemini
  calls exactly as to any cached function; hits never consume
  rate-limit capacity.
- **Usage/cost** — provider-reported token counts (input/output/total)
  are recorded when the API returns them, otherwise `null` (never
  estimated). With a configured `PricingConfig`, estimated cost and
  cache-hit savings appear in analytics; without pricing they stay
  `null`. Figures are estimates, not billing.
- **Errors** — missing key/SDK raises `GeminiConfigurationError` with a
  clear message; API failures raise `GeminiProviderError`. Messages
  never contain the key. The client is built lazily, so importing or
  constructing the provider never requires a key.
- **Dashboard** — Gemini activity (provider `gemini`, model, hits,
  tokens, costs) shows up in the existing dashboard/analytics with no
  extra endpoints; the key is never exposed through the API.

## Dashboard

Two optional layers sit beside the core library:

1. **FastAPI dashboard API** (`backend/api/`) — reads the same SQLite
   cache file your application writes and serves it as JSON.
2. **React dashboard** (`frontend/`) — a Vite + React + TypeScript UI.

Run them (from the project):

```powershell
# terminal 1 — API (from backend/)
python -m uvicorn api.main:app --port 8000

# terminal 2 — UI (from frontend/)
npm install
npm run dev
```

Open <http://localhost:5173>:

- **Dashboard** (`#/`) — status pill, metric cards (requests, hit rate,
  latency, …), cache entry table with Clear Cache, recent activity feed.
- **Analytics** (`#/analytics`) — summary cards, SVG charts (requests
  over time with hour/day toggle, latency, cost, hits vs misses bar),
  per-provider table, cost insights, Clear Analytics.

Unknown values render as `N/A`, never `0`. If the backend is
unreachable, an error banner with a retry action appears — no data is
ever fabricated. The header also shows the configured cache backend
(`Cache: SQLite` / `Cache: Redis`) as reported by `/api/health`.

## Redis cache backend

Redis is an optional second cache backend. SQLite remains the default:
existing code keeps working with zero changes and zero new dependencies.

```powershell
pip install "llmcachex-core[redis]"
```

Start local Redis with Docker (development only — no cloud, no key):

```powershell
docker run --name llmcachex-redis -p 6379:6379 -d redis:7-alpine
docker exec llmcachex-redis redis-cli ping
# PONG
```

```python
from llmcachex import cached_call

@cached_call(
    strategy="redis",
    redis_url="redis://localhost:6379/0",
    ttl_hours=24,
)
def generate(prompt):
    return external_api_call(prompt)

generate("hi")  # miss: executes, stores JSON in Redis
generate("hi")  # hit: function does NOT execute
```

Behavior notes:

- **Same semantics as SQLite** — deterministic request hashing (the
  identical logical hash), TTL (`ttl_seconds`/`ttl_hours`, UTC, invalid
  combinations rejected), exceptions never cached, hits bypass the rate
  limiter, analytics recorded identically.
- **JSON only, never pickle.** Values are JSON documents with
  `response`/`created_at`/`expires_at`/`request_hash` under the
  `llmcachex:cache:` namespace (`redis_prefix` overrides it).
- **Native TTL** (`SET … EX`) plus stored expiry metadata, so cache
  inspection matches SQLite; expired entries read as misses.
- **Safe clearing** — `clear()` SCANs the namespace and deletes only
  LLMCacheX keys. No `KEYS *`, no `FLUSHDB`/`FLUSHALL`: unrelated data
  is never touched.
- **No silent fallback** — if Redis is down, calls fail fast with an
  actionable `RedisStorageError` (never quietly switch backends).
- **Metrics/analytics** still use the local SQLite file selected by
  `cache_path`; only cached values live in Redis.
- **Security** — the URL may carry credentials for remote servers, but
  they are redacted in errors/logs and never exposed via the API,
  analytics or frontend. No API key is required for Redis itself.
- **Testing** — `pytest -m redis` runs the live integration suite
  (needs Redis on `localhost:6379`); the default suite uses fakes only.

## Semantic caching

Semantic caching is **opt-in**: `@cached_call()` alone always means
exact SHA-256 caching only. When enabled, an exact miss falls back to a
local similarity search — a sufficiently similar prior request returns
its cached response without executing the function.

```powershell
pip install "llmcachex-core[semantic]"
```

```python
from llmcachex import cached_call

@cached_call(
    semantic=True,
    semantic_threshold=0.92,
    semantic_fields=["prompt"],
    ttl_hours=24,
)
def generate(prompt):
    return external_api_call(prompt)

generate("Explain recursion in Python for a beginner.")  # miss: executes
generate("Can you explain Python recursion to a beginner?")  # semantic hit
```

How it works and what to know:

1. **Exact first** — the deterministic hash is always checked before
   any embedding is generated; exact hits never embed.
2. **Opt-in only** — `semantic=True` per decorator; nothing is global
   and existing applications behave exactly as before.
3. **Threshold** — `semantic_threshold` (`0`–`1`, default `0.92`);
   higher is stricter, lower matches more but risks incorrect reuse.
4. **Semantic fields** — `semantic_fields=["prompt"]` embeds only those
   bound arguments. Omitted, every argument is embedded (nothing is
   silently ignored) — prefer explicit fields so configuration like
   temperature cannot accidentally merge.
5. **Local model** — embeddings run on-device via `fastembed`
   (default `BAAI/bge-small-en-v1.5`, MIT weights, 384 dimensions);
   override with `semantic_model=` or `SEMANTIC_MODEL`. First use
   downloads weights once (cached locally); inference needs no network
   and **no API key**. The model loads lazily and is shared per name.
   (On Windows the library silences `huggingface_hub`'s cosmetic
   symlink-cache notice via `HF_HUB_DISABLE_SYMLINKS_WARNING` unless
   you set it yourself.)
6. **Compatibility** — matches require the same function, provider,
   model, embedding model + dimension, semantic fields and cache
   namespace; the API key is never embedded, hashed or stored.
7. **TTL** — candidates whose exact entry expired or was deleted are
   ineligible (stale rows are dropped lazily); `DELETE /api/cache`
   clears semantic rows too.
8. **Backends** — works with SQLite and Redis (exact values stay in
   Redis; the semantic index is a documented sidecar SQLite table next
   to metrics/analytics).
9. **Analytics** — semantic hits count as hits (`semantic_hits`
   tracked in `/api/stats` and `/api/analytics/summary`, plus a
   `Semantic Hit` activity row); cost savings reuse the avoided-cost
   logic. No fake metrics.
10. **Privacy** — embeddings may indirectly represent user input and
    are stored locally; prompts never leave the machine, and embeddings
    are never exposed through the API or dashboard.
11. **Scale** — the index is a bounded (default 1000 rows) linear scan
    over compatible candidates: intended for small-to-medium caches,
    not vector-database scale.

## API

All endpoints served by `http://localhost:8000` (read-only except the
two `DELETE`s):

| Method | Path                        | Description |
| ------ | --------------------------- | ----------- |
| GET    | `/api/health`               | Service liveness (`status`, `service`, `time`) |
| GET    | `/api/stats`                | Hit/miss counts, hit rate, stored/expired entries, retries, rate-limit events, latency |
| GET    | `/api/cache`                | Cache entry metadata (hash, age, TTL/expiry, hits) — never values |
| DELETE | `/api/cache`                | Clear cached entries; metrics and analytics are kept |
| GET    | `/api/activity`             | Recent activity events (hit/miss/execution/retry/rate-limit/failure) |
| GET    | `/api/analytics/summary`    | Aggregated analytics: requests, hit rate, latency, retries, tokens, estimated cost and savings (`null` = unknown) |
| GET    | `/api/analytics/timeseries` | Time buckets — `bucket=hour\|day` (default `hour`), `hours=1..8760` (default 24) |
| GET    | `/api/analytics/providers`  | Per-provider request/token/cost rollup |
| GET    | `/api/analytics/usage`      | Totals from provider-reported usage plus estimates |
| DELETE | `/api/analytics`            | Clear analytics history (cache and counters untouched) |

Interactive documentation: <http://localhost:8000/docs>.
CORS allows the local dev origin only (`localhost:5173`). Errors from an
unreadable cache database become a clean `503`, not a stack trace.

**API keys:** neither the core library nor this API requires any API key
— internal or external. Nothing in this project ever asks for OpenAI,
Gemini, Anthropic, GitHub, PyPI or cloud credentials (a PyPI token would
only ever be needed for an actual release publish, which has not
happened and is not performed by this documentation).

## Configuration

### Decorator options

| Option | Default | Meaning |
| ------ | ------- | ------- |
| `cache_path` | `"llmcachex.db"` | SQLite database file (created on demand; under `strategy="redis"` it holds metrics/analytics while values live in Redis) |
| `strategy` | `"sqlite"` | Cache backend: `"sqlite"` or `"redis"` (redis needs the `redis` extra + a server; unknown values raise, never fall back) |
| `redis_url` | `"redis://localhost:6379/0"` | Redis URL for `strategy="redis"` (credentials redacted everywhere) |
| `redis_prefix` | `"llmcachex:cache:"` | Redis key namespace; clearing only removes this namespace |
| `ttl_hours` / `ttl_seconds` | none | Expiration; supply at most one, must be positive |
| `retries` | `0` | Extra attempts after the first failure |
| `backoff_factor` | `0.5` | Exponential backoff base (seconds) |
| `rate_limit` | none | Max executions per `rate_period` |
| `rate_period` | `60.0` | Sliding-window length (seconds) |
| `provider` | none | Provider label: string or `ProviderAdapter` |
| `model` | none | Model label (analytics only) |
| `usage` | none | Callable extracting token counts from your result |
| `pricing` | none | `PricingConfig` with *your* per-1k prices |

Invalid combinations raise `ValueError` at decoration time.

### Environment variables

| Variable | Used by | Default |
| -------- | ------- | ------- |
| `LLMCACHEX_CACHE_DB` | Dashboard API | `llmcachex.db` (relative to working dir) |
| `CACHE_STRATEGY` | Dashboard API | `sqlite` (`sqlite` or `redis`) |
| `REDIS_URL` | Dashboard API (redis strategy) | `redis://localhost:6379/0` |
| `REDIS_KEY_PREFIX` | Dashboard API (redis strategy) | `llmcachex:cache:` |
| `VITE_API_BASE_URL` | React frontend (`frontend/.env`) | `http://localhost:8000` |

## Security

For the current release (no provider integrations, no auth, local
usage), the practical rules are:

- **Never commit API keys** or secrets to the repository — and never
  hardcode them in source.
- **Never put secrets in frontend `VITE_*` variables.** Anything
  prefixed `VITE_` is compiled into public browser JavaScript. The
  frontend's only variable (`VITE_API_BASE_URL`) is a local URL — keep
  it that way.
- **Never expose provider credentials through the dashboard API.** The
  API serves cache/analytics metadata only; do not add secret material
  to event data or cache entries.
- When future provider integrations arrive (see [Roadmap](#roadmap)),
  supply credentials via **environment variables or a secure secret
  manager** — never in code, notebooks, logs or the frontend.

No secrets are used, stored or required anywhere in this project today.

## Development

Layout and responsibilities (three clearly separated layers):

```text
backend/src/llmcachex/   → core library (stdlib only, zero deps, no FastAPI)
backend/api/             → optional FastAPI layer (installed via [api] extra)
backend/tests/           → pytest suite
frontend/                → React dashboard (Vite/TS, consumes the API)
```

Common commands (from the project root, using `.venv`):

```powershell
# install for development
.\.venv\Scripts\python.exe -m pip install -e "./backend[dev]"

# run the API
cd backend; ..\.venv\Scripts\python.exe -m uvicorn api.main:app --port 8000

# frontend (from frontend/)
npm install
npm run dev        # dev server on :5173
npm run build      # tsc -b + vite build
npm run lint       # oxlint
```

## Testing

```powershell
# from the project root
.\.venv\Scripts\python.exe -m pytest backend
```

The suite (313 tests) covers hashing, SQLite/Redis storage, Redis storage
(with fakes), decorator hit/miss behavior, TTL, retry/backoff, rate
limiting, metrics, analytics aggregation, provider registry, edge
cases, concurrency and every API endpoint (FastAPI test client).
`pytest` is a development dependency only — the installed library has
zero required dependencies.

Live Redis integration tests are marked and skipped without a server:

```powershell
# needs Redis on localhost:6379 (see "Redis cache backend")
.\.venv\Scripts\python.exe -m pytest backend -m redis
```

Static checks:

```powershell
# backend (pyflakes + critical pycodestyle via ruff)
.\.venv\Scripts\python.exe -m ruff check backend

# frontend
cd frontend; npm run lint
```

## Architecture

How a decorated call flows through the system:

```text
        Developer Function
                │
                ▼
           @cached_call
                │
                ▼
             Hasher  ──── SHA-256 of normalized arguments
                │
                ▼
          SQLite Cache
           │        │
        HIT        MISS
           │        │
           ▼        ▼
        return   Rate Limiter (optional; hits never pass through here)
                    │
                    ▼
            Retry / Backoff
                    │
                    ▼
            Function Execution
                    │
                    ▼
             Cache Result
                    │
                    ▼
               Analytics  ──── events, usage, cost, savings
                    │
                    ▼
             Dashboard/API  ──── reads the same SQLite file (read-only)
```

**The three layers, separated on purpose:**

1. **Core library** (`backend/src/llmcachex/`) — hashing, storage,
   decorator, resilience, metrics, analytics, providers. **Standard
   library only**; installable and usable completely on its own. It
   never imports FastAPI, React or anything browser-related.
2. **Dashboard API** (`backend/api/`) — a thin FastAPI layer around the
   same SQLite cache file. Installed via the `api` extra; never
   imported by the core library.
3. **React dashboard** (`frontend/`) — consumes the API over HTTP; has
   no cache logic and no direct database access.

Data flows one way: your application writes to SQLite (through the core
library); the API reads SQLite; the frontend reads the API.

## Limitations

- **Single-process cache** — SQLite storage is thread-safe within one
  process (internal lock) but is not designed for multi-process or
  multi-machine sharing.
- **Process-local rate limiting** — the sliding-window limiter lives in
  memory: not distributed, not persisted across restarts.
- **Provider/model join the cache key when configured** — passing
  `provider=`/`model=` (or a provider adapter) scopes entries to that
  identity, so different models never share responses. Provider-free
  calls keep the historical key. Annotate per-function accordingly (one
  function = one logical provider/model).
- **Estimates, not billing** — cost figures come from your configured
  prices and reported usage; they are estimates, labeled as such.
- **No token estimation** — token counts are only ever taken from
  provider-reported usage you extract; no tokenizer is bundled.
- **Bounded analytics** — the activity log keeps 200 events, analytics
  keeps 1000 (oldest trimmed); this is deliberate for a local tool.
- **Function identity is `module.qualname` + arguments** — the cache key
  is built from the wrapped function's module, qualified name and bound
  arguments, plus the provider/model identity when one is configured
  (never secrets). Closures/lambdas that share a qualname and are called with
  equal arguments therefore share one cache entry; use distinct named
  functions when calls must not be shared.
- **Analytics recording is best-effort** — if an analytics/activity
  write fails (locked database, disk full), the cached value is still
  returned and a warning is logged; core cache reads/writes still raise.
- **No auth, not internet-facing** — the dashboard binds local dev
  origins (CORS for `localhost:5173`); it is a developer tool, not a
  multi-tenant service.
- **Not implemented (by design this phase):** Redis/distributed cache,
  semantic caching, ML, provider SDK integrations, cloud deployment.

## Roadmap

*Future possibilities — none of these are implemented in this release:*

1. **Provider adapters** — optional integrations with specific LLM
   vendors (behind extras, opt-in).
2. **Real LLM usage extraction** — ready-made usage extractors for
   common response formats.
3. **Provider-aware cost tracking** — richer per-model pricing presets
   (still user-supplied and overrideable).
4. **Redis / distributed cache** — a shared storage backend for
   multi-process deployments.
5. **Semantic caching** — similarity-based lookups (would require an
   ML/embedding dependency; explicitly out of scope so far).
6. **Authentication** — for exposing the dashboard beyond localhost.
7. **Multi-user deployment** — per-user cache namespaces and quotas.
8. **Cloud deployment** — hosted dashboard option.
9. **Advanced monitoring** — OpenTelemetry/Prometheus export.
10. **More storage backends** — e.g. PostgreSQL, filesystem-per-entry.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Alimehdi Suleman.
