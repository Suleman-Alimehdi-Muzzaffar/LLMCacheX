# LLMCacheX — Developer Dashboard (Frontend)

This is the developer dashboard for LLMCacheX, built with React, Vite, and
TypeScript.

> **Status:** Phase 7 — dashboard + analytics page connected to the local
> backend API. The dashboard shows live cache statistics, cache entry
> metadata, and recent activity; the analytics page shows historical
> requests, latency/cost timeseries, per-provider rollups and cache
> savings — all fetched from `http://localhost:8000`. No values on the
> page are hardcoded or simulated.

## Relationship to the backend

- The core LLM caching implementation lives in `../backend`
  (the reusable `llmcachex` Python package plus the dashboard API in
  `backend/api/`).
- This frontend contains **no** cache logic. It only consumes data from
  the backend API (health, cache statistics, cache entries, activity
  events, analytics).
- If the backend is unreachable, the page shows an error banner with a
  retry action instead of fabricating data.
- Unknown values (tokens, cost, latency when there is no data) are
  displayed as `N/A` — never as `0`.

## What the dashboard shows

- **Header** — connection status pill (from `/api/health`) and a Refresh
  action.
- **Metric cards** — total requests, hits, misses, hit rate, stored
  entries, retry count, rate-limit events, average/latest execution
  latency (from `/api/stats`).
- **Cache table** — entry metadata: hash, key summary, age, TTL/expiry,
  hit count. Cache **values are never displayed** (the API does not
  expose them). Includes Refresh and a `Clear Cache` action
  (`DELETE /api/cache`, which keeps metrics).
- **Activity list** — the most recent events: `cache_hit`, `cache_miss`,
  `function_execution`, `retry`, `rate_limit`, `execution_failed`
  (from `/api/activity`).

## What the analytics page shows (`#/analytics`)

- **Summary cards** — requests, hit rate, hits/misses, average
  execution latency, retries/failures, rate-limit events, total tokens,
  estimated cost and estimated cost saved (from
  `/api/analytics/summary`).
- **Charts** (lightweight inline SVG, no chart library): requests over
  time with an hourly/daily bucket toggle, latency over time, cost over
  time, and a stacked hits-vs-misses bar (from
  `/api/analytics/timeseries`).
- **Providers table** — per-provider requests, hits, misses, tokens and
  estimated cost (`/api/analytics/providers`); missing provider
  metadata shows as an "Unknown" badge.
- **Cost insights** — totals plus the note that cost figures are
  estimates from configured pricing, not billing
  (`/api/analytics/usage`), and a **Clear Analytics** action
  (`DELETE /api/analytics`).
- App navigation between Dashboard and Analytics uses hash routes
  (`#/`, `#/analytics`) — no router dependency.

## Configuration

The API base URL comes from the environment file:

```text
frontend/.env
VITE_API_BASE_URL=http://localhost:8000
```

Change it there if the backend runs on a different host/port; the backend
must allow the frontend origin (CORS origins are configured in
`backend/api/main.py`).

## Development

Start the backend first, then the frontend:

```powershell
# terminal 1 (from backend/)
python -m uvicorn api.main:app --port 8000

# terminal 2 (from frontend/)
npm install
npm run dev
```

Open <http://localhost:5173>. Build and lint checks:

```powershell
npm run build   # tsc -b + vite build
npm run lint    # oxlint
```

## Layout

```text
frontend/
├── .env                     # VITE_API_BASE_URL
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx              # app shell
│   ├── App.css              # layout + component styles
│   ├── index.css            # theme tokens + base styles
│   ├── types/api.ts         # response types mirroring the API schemas
│   ├── services/api.ts      # fetch client + ApiError + analytics calls
│   ├── hooks/
│   │   └── useHashRoute.ts  # minimal hash routing (#/, #/analytics)
│   ├── components/
│   │   ├── MetricCard.tsx
│   │   ├── CacheTable.tsx
│   │   ├── ActivityList.tsx
│   │   └── charts/
│   │       ├── LineChart.tsx    # SVG time-series chart (nulls = gaps)
│   │       └── HitMissBar.tsx   # stacked hits vs misses bar
│   └── pages/
│       ├── Dashboard.tsx    # data fetching + composition
│       └── Analytics.tsx    # analytics summary, charts, providers
└── package.json
```
