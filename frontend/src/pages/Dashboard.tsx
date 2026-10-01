/**
 * LLMCacheX developer dashboard.
 *
 * Every number on this page is fetched from the local backend API —
 * nothing is hardcoded or simulated. When the backend is unreachable the
 * page shows a clear error state with a retry action.
 */

import { useCallback, useEffect, useState } from 'react'

import ActivityList from '../components/ActivityList'
import CacheTable from '../components/CacheTable'
import MetricCard from '../components/MetricCard'
import { ApiError, apiBaseUrl, clearCache, getActivity, getCacheEntries, getHealth, getStats } from '../services/api'
import type {
  ActivityEvent,
  CacheEntry,
  HealthResponse,
  StatsResponse,
} from '../types/api'

interface DashboardData {
  health: HealthResponse
  stats: StatsResponse
  cacheEntries: CacheEntry[]
  activity: ActivityEvent[]
}

async function fetchAll(): Promise<DashboardData> {
  const [health, stats, cache, activity] = await Promise.all([
    getHealth(),
    getStats(),
    getCacheEntries(),
    getActivity(100),
  ])
  return {
    health,
    stats,
    cacheEntries: cache.entries,
    activity: activity.events,
  }
}

function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message
  return 'Unexpected error while contacting the backend.'
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function formatMs(value: number): string {
  if (value > 0 && value < 1) return `${value.toFixed(2)} ms`
  return `${value.toFixed(1)} ms`
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (isRefresh: boolean) => {
    if (isRefresh) setRefreshing(true)
    else setLoading(true)
    setError(null)
    try {
      setData(await fetchAll())
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  // Initial load (async-first; state updates happen after the request).
  useEffect(() => {
    let active = true
    ;(async () => {
      try {
        const result = await fetchAll()
        if (active) {
          setData(result)
          setError(null)
        }
      } catch (err) {
        if (active) setError(messageOf(err))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [])

  const handleClear = useCallback(async () => {
    setClearing(true)
    setError(null)
    try {
      await clearCache()
      await load(true)
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'Failed to clear the cache.',
      )
    } finally {
      setClearing(false)
    }
  }, [load])

  const stats = data?.stats
  const backendLive = data?.health.status === 'ok'
  const cacheBackend = data?.health.cache_backend === 'redis' ? 'Redis' : 'SQLite'

  return (
    <div className="dashboard">
      <header className="topbar">
        <div>
          <h2>Dashboard</h2>
          <p className="topbar__subtitle">
            Cache statistics, entries and runtime activity
          </p>
        </div>
        <div className="topbar__status">
          <span
            className={`pill ${
              error
                ? 'pill--err'
                : backendLive
                  ? 'pill--ok'
                  : 'pill--muted'
            }`}
            title={apiBaseUrl}
          >
            <span className="pill__dot" aria-hidden="true" />
            {error ? 'Backend unreachable' : backendLive ? 'Connected' : 'Connecting…'}
          </span>
          <span className="pill pill--muted" title="Cache backend reported by the API">
            <span className="pill__dot" aria-hidden="true" />
            {backendLive ? `Cache: ${cacheBackend}` : 'Cache: —'}
          </span>
          <button
            type="button"
            className="btn"
            onClick={() => void load(true)}
            disabled={refreshing || loading}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {error ? (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="btn"
            onClick={() => void load(Boolean(data))}
          >
            Retry
          </button>
        </div>
      ) : null}

      {loading && !data ? (
        <p className="panel__empty">Loading dashboard…</p>
      ) : stats ? (
        <>
          <section className="metrics" aria-label="Cache statistics">
            <MetricCard
              label="Total Requests"
              value={stats.total_requests.toLocaleString()}
              hint="hits + misses"
            />
            <MetricCard
              label="Cache Hits"
              value={stats.cache_hits.toLocaleString()}
              tone="good"
              hint="served from SQLite"
            />
            <MetricCard
              label="Cache Misses"
              value={stats.cache_misses.toLocaleString()}
              hint="executed the function"
            />
            <MetricCard
              label="Hit Rate"
              value={formatPercent(stats.hit_rate)}
              tone={stats.hit_rate >= 0.5 ? 'good' : 'default'}
              hint="hits / total"
            />
            <MetricCard
              label="Stored Entries"
              value={stats.stored_entries.toLocaleString()}
              hint={`${stats.expired_entries} expired`}
            />
            <MetricCard
              label="Retry Count"
              value={stats.retry_count.toLocaleString()}
              hint={`${stats.failed_calls} failed calls`}
            />
            <MetricCard
              label="Rate-Limit Events"
              value={stats.rate_limit_events.toLocaleString()}
              hint="executions delayed"
            />
            <MetricCard
              label="Avg Execution Latency"
              value={formatMs(stats.avg_execution_latency_ms)}
              hint={`latest ${formatMs(stats.latest_execution_latency_ms)}`}
            />
          </section>

          <div className="dashboard__grid">
            <CacheTable
              entries={data?.cacheEntries ?? []}
              loading={loading || refreshing}
              clearing={clearing}
              onRefresh={() => void load(true)}
              onClear={() => void handleClear()}
            />
            <ActivityList
              events={data?.activity ?? []}
              loading={loading || refreshing}
            />
          </div>
        </>
      ) : null}
    </div>
  )
}
