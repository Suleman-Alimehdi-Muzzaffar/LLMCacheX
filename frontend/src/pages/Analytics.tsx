/**
 * LLMCacheX analytics page.
 *
 * Every value is fetched from the local backend's analytics API.
 * `null` backend values render as "N/A" — unknown token/cost data is
 * never displayed as zero. Charts are fed exclusively by backend
 * timeseries data (no chart library; lightweight inline SVG).
 */

import { useCallback, useEffect, useState } from 'react'

import HitMissBar from '../components/charts/HitMissBar'
import LineChart from '../components/charts/LineChart'
import MetricCard from '../components/MetricCard'
import {
  ApiError,
  clearAnalytics,
  getAnalyticsSummary,
  getAnalyticsTimeseries,
  getProviderAnalytics,
  getUsageAnalytics,
} from '../services/api'
import type {
  AnalyticsSummary,
  ProviderAnalytics,
  TimeseriesResponse,
  UsageAnalytics,
} from '../types/api'

interface AnalyticsData {
  summary: AnalyticsSummary
  timeseries: TimeseriesResponse
  providers: ProviderAnalytics[]
  usage: UsageAnalytics
}

type Bucket = 'hour' | 'day'

const NA = 'N/A'

const COLOR_REQUESTS = '#4c8dff'
const COLOR_HITS = '#3fb950'
const COLOR_MISSES = '#d29922'

function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message
  return 'Unexpected error while contacting the backend.'
}

async function fetchData(bucket: Bucket): Promise<AnalyticsData> {
  const [summary, timeseries, providers, usage] = await Promise.all([
    getAnalyticsSummary(),
    getAnalyticsTimeseries(bucket, bucket === 'day' ? 168 : 24),
    getProviderAnalytics(),
    getUsageAnalytics(),
  ])
  return { summary, timeseries, providers, usage }
}

function displayNumber(value: number | null): string {
  return value === null ? NA : value.toLocaleString()
}

function displayCost(value: number | null): string {
  return value === null ? NA : String(value)
}

function displayMs(value: number | null): string {
  if (value === null) return NA
  if (value > 0 && value < 1) return `${value.toFixed(2)} ms`
  return `${value.toFixed(1)} ms`
}

function displayPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function bucketLabel(timestamp: string, bucket: Bucket): string {
  // ISO UTC timestamps: "2026-09-25T14:00:00+00:00"
  return bucket === 'day' ? timestamp.slice(5, 10) : timestamp.slice(11, 16)
}

export default function Analytics() {
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [bucket, setBucket] = useState<Bucket>('hour')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (isRefresh: boolean, activeBucket: Bucket) => {
    if (isRefresh) setRefreshing(true)
    else setLoading(true)
    setError(null)
    try {
      setData(await fetchData(activeBucket))
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    ;(async () => {
      try {
        const result = await fetchData('hour')
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

  const handleBucket = useCallback(
    (next: Bucket) => {
      setBucket(next)
      void load(true, next)
    },
    [load],
  )

  const handleClear = useCallback(async () => {
    setClearing(true)
    setError(null)
    try {
      await clearAnalytics()
      await load(true, bucket)
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'Failed to clear analytics history.',
      )
    } finally {
      setClearing(false)
    }
  }, [bucket, load])

  const summary = data?.summary
  const points = data?.timeseries.points ?? []

  const requestSeries = [
    {
      name: 'Requests',
      color: COLOR_REQUESTS,
      points: points.map((p) => ({
        label: bucketLabel(p.timestamp, bucket),
        value: p.requests,
      })),
    },
    {
      name: 'Cache hits',
      color: COLOR_HITS,
      points: points.map((p) => ({
        label: bucketLabel(p.timestamp, bucket),
        value: p.cache_hits,
      })),
    },
    {
      name: 'Cache misses',
      color: COLOR_MISSES,
      points: points.map((p) => ({
        label: bucketLabel(p.timestamp, bucket),
        value: p.cache_misses,
      })),
    },
  ]

  const latencySeries = [
    {
      name: 'Avg execution latency (ms)',
      color: COLOR_REQUESTS,
      points: points.map((p) => ({
        label: bucketLabel(p.timestamp, bucket),
        value: p.average_latency_ms,
      })),
    },
  ]

  const costSeries = [
    {
      name: 'Estimated cost',
      color: COLOR_HITS,
      points: points.map((p) => ({
        label: bucketLabel(p.timestamp, bucket),
        value: p.estimated_cost,
      })),
    },
  ]

  const hasLatencyData = points.some((p) => p.average_latency_ms !== null)
  const hasCostData = points.some((p) => p.estimated_cost !== null)

  return (
    <div className="dashboard">
      <header className="topbar">
        <div>
          <h2>Analytics</h2>
          <p className="topbar__subtitle">
            Historical runtime insights · provider usage · cost estimates
          </p>
        </div>
        <div className="topbar__status">
          <div className="segmented" role="group" aria-label="Time bucket">
            <button
              type="button"
              className={`segmented__btn ${bucket === 'hour' ? 'segmented__btn--active' : ''}`}
              onClick={() => handleBucket('hour')}
              disabled={loading || refreshing}
            >
              Hourly
            </button>
            <button
              type="button"
              className={`segmented__btn ${bucket === 'day' ? 'segmented__btn--active' : ''}`}
              onClick={() => handleBucket('day')}
              disabled={loading || refreshing}
            >
              Daily
            </button>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => void load(true, bucket)}
            disabled={refreshing || loading}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => void handleClear()}
            disabled={clearing || loading}
          >
            {clearing ? 'Clearing…' : 'Clear Analytics'}
          </button>
        </div>
      </header>

      {error ? (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="btn"
            onClick={() => void load(Boolean(data), bucket)}
          >
            Retry
          </button>
        </div>
      ) : null}

      {loading && !data ? (
        <p className="panel__empty">Loading analytics…</p>
      ) : summary ? (
        <>
          <section className="metrics" aria-label="Analytics overview">
            <MetricCard
              label="Total Requests"
              value={summary.total_requests.toLocaleString()}
              hint="hits + misses"
            />
            <MetricCard
              label="Cache Hit Rate"
              value={displayPercent(summary.hit_rate)}
              tone={summary.hit_rate >= 0.5 ? 'good' : 'default'}
              hint="hits / total"
            />
            <MetricCard
              label="Cache Hits"
              value={summary.cache_hits.toLocaleString()}
              tone="good"
            />
            <MetricCard
              label="Semantic Hits"
              value={(summary.semantic_hits ?? 0).toLocaleString()}
              tone="good"
              hint="similarity reuse, subset of hits"
            />
            <MetricCard
              label="Cache Misses"
              value={summary.cache_misses.toLocaleString()}
            />
            <MetricCard
              label="Average Latency"
              value={displayMs(summary.average_latency_ms)}
              hint="executions only"
            />
            <MetricCard
              label="Retries"
              value={summary.retry_count.toLocaleString()}
              hint={`${summary.failed_requests} failed requests`}
            />
            <MetricCard
              label="Rate-Limit Events"
              value={summary.rate_limit_events.toLocaleString()}
            />
            <MetricCard
              label="Total Tokens"
              value={displayNumber(summary.total_tokens)}
              hint={
                summary.total_tokens === null ? 'not available' : 'provider-reported'
              }
            />
            <MetricCard
              label="Estimated Cost"
              value={displayCost(summary.estimated_cost)}
              hint={summary.estimated_cost === null ? 'not available' : 'estimate'}
            />
            <MetricCard
              label="Estimated Cost Saved"
              value={displayCost(summary.estimated_cost_saved)}
              hint={
                summary.estimated_cost_saved === null
                  ? 'not available'
                  : 'avoided by cache hits'
              }
            />
          </section>

          <div className="dashboard__grid">
            <section className="panel">
              <div className="panel__header">
                <h2>Requests over time</h2>
                <span className="panel__subtitle">
                  {bucket === 'hour' ? 'last 24 hours' : 'last 7 days'} ·{' '}
                  {data?.timeseries.count ?? 0} buckets
                </span>
              </div>
              <div className="panel__body">
                {points.length === 0 ? (
                  <p className="panel__empty">No activity in this window.</p>
                ) : (
                  <LineChart series={requestSeries} emptyMessage="No activity yet" />
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Cache hits vs misses</h2>
                <span className="panel__subtitle">all retained requests</span>
              </div>
              <div className="panel__body">
                <HitMissBar hits={summary.cache_hits} misses={summary.cache_misses} />
              </div>
            </section>
          </div>

          <div className="dashboard__grid">
            <section className="panel">
              <div className="panel__header">
                <h2>Latency over time</h2>
                <span className="panel__subtitle">execution latency (ms)</span>
              </div>
              <div className="panel__body">
                {hasLatencyData ? (
                  <LineChart series={latencySeries} />
                ) : (
                  <p className="chart-empty">
                    No execution latency recorded yet.
                  </p>
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Cost over time</h2>
                <span className="panel__subtitle">estimates, not billing</span>
              </div>
              <div className="panel__body">
                {hasCostData ? (
                  <LineChart series={costSeries} />
                ) : (
                  <p className="chart-empty">
                    No cost data — requires usage and pricing information.
                  </p>
                )}
              </div>
            </section>
          </div>

          <div className="dashboard__grid">
            <section className="panel">
              <div className="panel__header">
                <h2>Providers</h2>
                <span className="panel__subtitle">
                  {(data?.providers.length ?? 0) > 0
                    ? `${data?.providers.length} provider(s)`
                    : 'no provider metadata yet'}
                </span>
              </div>
              {(data?.providers.length ?? 0) === 0 ? (
                <p className="panel__empty">
                  No requests recorded yet. Provider names appear here once
                  functions are annotated with provider metadata.
                </p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Provider</th>
                        <th>Requests</th>
                        <th>Hits</th>
                        <th>Misses</th>
                        <th>Tokens</th>
                        <th>Est. Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data?.providers.map((provider) => (
                        <tr key={provider.provider}>
                          <td>
                            {provider.provider === 'unknown' ? (
                              <span className="badge badge--warn">Unknown</span>
                            ) : (
                              <span className="badge badge--info">
                                {provider.provider}
                              </span>
                            )}
                          </td>
                          <td>{provider.requests.toLocaleString()}</td>
                          <td>{provider.cache_hits.toLocaleString()}</td>
                          <td>{provider.cache_misses.toLocaleString()}</td>
                          <td>{displayNumber(provider.total_tokens)}</td>
                          <td>{displayCost(provider.estimated_cost)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="panel">
              <div className="panel__header">
                <h2>Cost insights</h2>
                <span className="panel__subtitle">estimate only</span>
              </div>
              <div className="panel__body cost-insight">
                <div className="metrics">
                  <MetricCard
                    label="Estimated Cost"
                    value={displayCost(summary.estimated_cost)}
                    hint={
                      summary.estimated_cost === null
                        ? 'usage/pricing unknown'
                        : 'across executed requests'
                    }
                  />
                  <MetricCard
                    label="Estimated Cost Saved"
                    value={displayCost(summary.estimated_cost_saved)}
                    hint={
                      summary.estimated_cost_saved === null
                        ? 'cost of hits unknown'
                        : 'avoided via cache hits'
                    }
                  />
                  <MetricCard
                    label="Usage"
                    value={
                      data?.usage.input_tokens === null
                        ? NA
                        : `${displayNumber(data?.usage.input_tokens ?? null)} in / ${displayNumber(data?.usage.output_tokens ?? null)} out`
                    }
                    hint="provider-reported tokens"
                  />
                </div>
                <p className="cost-note">
                  Cost estimates require provider usage and pricing
                  information. Figures are estimates from your configuration —
                  not actual billing. Unknown values show as {NA}, never as 0.
                </p>
              </div>
            </section>
          </div>
        </>
      ) : (
        <p className="panel__empty">No analytics data.</p>
      )}
    </div>
  )
}
