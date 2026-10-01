/**
 * Cache entries table — metadata only (hash / timestamps / status).
 * Response payloads are never requested from or shown by the frontend.
 */

import type { CacheEntry } from '../types/api'

interface CacheTableProps {
  entries: CacheEntry[]
  loading: boolean
  clearing: boolean
  onRefresh: () => void
  onClear: () => void
}

function formatTimestamp(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

export default function CacheTable({
  entries,
  loading,
  clearing,
  onRefresh,
  onClear,
}: CacheTableProps) {
  return (
    <section className="panel" aria-labelledby="cache-heading">
      <header className="panel__header">
        <div>
          <h2 id="cache-heading">Cache Entries</h2>
          <span className="panel__subtitle">
            {entries.length} stored · metadata only
          </span>
        </div>
        <div className="panel__actions">
          <button
            type="button"
            className="btn"
            onClick={onRefresh}
            disabled={loading || clearing}
          >
            Refresh
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={onClear}
            disabled={clearing || entries.length === 0}
          >
            {clearing ? 'Clearing…' : 'Clear Cache'}
          </button>
        </div>
      </header>

      {entries.length === 0 ? (
        <p className="panel__empty">
          {loading ? 'Loading cache entries…' : 'No cache entries yet.'}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Request Hash</th>
                <th scope="col">Created</th>
                <th scope="col">Expires</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.request_hash}>
                  <td className="table__hash" title={entry.request_hash}>
                    {entry.request_hash.slice(0, 16)}…
                  </td>
                  <td>{formatTimestamp(entry.created_at)}</td>
                  <td>
                    {entry.expires_at
                      ? formatTimestamp(entry.expires_at)
                      : '—'}
                  </td>
                  <td>
                    <span
                      className={`badge ${
                        entry.expired ? 'badge--warn' : 'badge--ok'
                      }`}
                    >
                      {entry.expired ? 'Expired' : 'Active'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
