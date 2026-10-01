/** Two-segment stacked bar: cache hits vs misses (real backend counts). */

interface HitMissBarProps {
  hits: number
  misses: number
}

export default function HitMissBar({ hits, misses }: HitMissBarProps) {
  const total = hits + misses
  if (total === 0) {
    return <p className="chart-empty">No requests recorded yet.</p>
  }
  const hitPercent = (hits / total) * 100
  const missPercent = 100 - hitPercent

  return (
    <div className="hitmiss">
      <div className="hitmiss__track" aria-hidden="true">
        {hits > 0 ? (
          <span className="hitmiss__seg hitmiss__seg--hit" style={{ width: `${hitPercent}%` }} />
        ) : null}
        {misses > 0 ? (
          <span className="hitmiss__seg hitmiss__seg--miss" style={{ width: `${missPercent}%` }} />
        ) : null}
      </div>
      <div className="hitmiss__legend">
        <span className="chart__legend-item">
          <span className="chart__swatch chart__swatch--hit" aria-hidden="true" />
          {hits.toLocaleString()} hits ({hitPercent.toFixed(1)}%)
        </span>
        <span className="chart__legend-item">
          <span className="chart__swatch chart__swatch--miss" aria-hidden="true" />
          {misses.toLocaleString()} misses ({missPercent.toFixed(1)}%)
        </span>
      </div>
    </div>
  )
}
