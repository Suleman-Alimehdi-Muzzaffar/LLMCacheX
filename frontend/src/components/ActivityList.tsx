/**
 * Recent runtime activity feed — event metadata only, never payloads.
 */

import type { ActivityEvent } from '../types/api'

interface ActivityListProps {
  events: ActivityEvent[]
  loading: boolean
}

const TYPE_LABELS: Record<string, string> = {
  cache_hit: 'Cache Hit',
  semantic_hit: 'Semantic Hit',
  cache_miss: 'Cache Miss',
  function_execution: 'Function Execution',
  execution_failed: 'Execution Failed',
  retry: 'Retry',
  rate_limit: 'Rate Limit',
}

const TYPE_TONES: Record<string, string> = {
  cache_hit: 'badge--ok',
  semantic_hit: 'badge--ok',
  cache_miss: 'badge--info',
  function_execution: 'badge--info',
  execution_failed: 'badge--err',
  retry: 'badge--warn',
  rate_limit: 'badge--warn',
}

function formatTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString()
}

function shortenFunction(name: string): string {
  // "__main__.module.outer.func" -> "outer.func" is enough context.
  const parts = name.split('.')
  return parts.length > 2 ? parts.slice(-2).join('.') : name
}

export default function ActivityList({ events, loading }: ActivityListProps) {
  return (
    <section className="panel" aria-labelledby="activity-heading">
      <header className="panel__header">
        <div>
          <h2 id="activity-heading">Recent Activity</h2>
          <span className="panel__subtitle">
            {events.length} event{events.length === 1 ? '' : 's'} · newest
            first
          </span>
        </div>
      </header>

      {events.length === 0 ? (
        <p className="panel__empty">
          {loading ? 'Loading activity…' : 'No activity recorded yet.'}
        </p>
      ) : (
        <ul className="activity">
          {events.map((event, index) => (
            <li className="activity__row" key={`${event.timestamp}-${index}`}>
              <span
                className={`badge ${TYPE_TONES[event.type] ?? 'badge--info'}`}
              >
                {TYPE_LABELS[event.type] ?? event.type}
              </span>
              <span className="activity__function">
                {shortenFunction(event.function)}
              </span>
              <span className="activity__time">{formatTime(event.timestamp)}</span>
              <span className="activity__latency">
                {event.latency_ms != null
                  ? `${event.latency_ms.toFixed(2)} ms`
                  : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
