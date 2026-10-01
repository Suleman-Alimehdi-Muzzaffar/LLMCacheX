/** Compact metric card used across the dashboard header row. */

interface MetricCardProps {
  label: string
  value: string
  hint?: string
  tone?: 'default' | 'good' | 'warn'
}

export default function MetricCard({
  label,
  value,
  hint,
  tone = 'default',
}: MetricCardProps) {
  return (
    <div className={`metric-card metric-card--${tone}`}>
      <span className="metric-card__label">{label}</span>
      <span className="metric-card__value">{value}</span>
      {hint ? <span className="metric-card__hint">{hint}</span> : null}
    </div>
  )
}
