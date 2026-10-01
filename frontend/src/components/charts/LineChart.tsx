/**
 * Lightweight SVG line chart — no chart library dependency.
 *
 * Series points carry explicit labels (time buckets from the backend).
 * `null` values render as gaps: the chart never invents data between
 * buckets. All values come from backend responses.
 */

export interface ChartPoint {
  label: string
  value: number | null
}

export interface ChartSeries {
  name: string
  color: string
  points: ChartPoint[]
}

interface LineChartProps {
  series: ChartSeries[]
  height?: number
  formatValue?: (value: number) => string
  emptyMessage?: string
}

const PAD_TOP = 12
const PAD_BOTTOM = 26
const PAD_LEFT = 44
const PAD_RIGHT = 10

export default function LineChart({
  series,
  height = 200,
  formatValue = (value) =>
    value >= 100 ? Math.round(value).toString() : String(value),
  emptyMessage = 'No data yet',
}: LineChartProps) {
  const allValues = series.flatMap((s) => s.points.map((p) => p.value))
  const known = allValues.filter((v): v is number => v !== null)

  if (known.length === 0) {
    return <p className="chart-empty">{emptyMessage}</p>
  }

  const labels = series[0]?.points.map((p) => p.label) ?? []
  const count = labels.length
  const width = 720
  const plotW = width - PAD_LEFT - PAD_RIGHT
  const plotH = height - PAD_TOP - PAD_BOTTOM

  let min = Math.min(...known, 0)
  let max = Math.max(...known)
  if (max === min) {
    max = min + 1
  }

  const xAt = (index: number) =>
    count <= 1
      ? PAD_LEFT + plotW / 2
      : PAD_LEFT + (index * plotW) / (count - 1)
  const yAt = (value: number) =>
    PAD_TOP + plotH - ((value - min) / (max - min)) * plotH

  // One path per contiguous run of non-null values (gaps at nulls).
  const paths = series.map((s) => {
    const segments: string[] = []
    let current: string[] = []
    s.points.forEach((point, index) => {
      if (point.value === null) {
        if (current.length > 0) segments.push(current.join(' '))
        current = []
        return
      }
      const cmd = current.length === 0 ? 'M' : 'L'
      current.push(`${cmd} ${xAt(index).toFixed(1)} ${yAt(point.value).toFixed(1)}`)
    })
    if (current.length > 0) segments.push(current.join(' '))
    return segments
  })

  const yTicks = [min, (min + max) / 2, max]
  const xLabelIndexes = Array.from(
    new Set([0, Math.floor((count - 1) / 2), count - 1]),
  ).filter((i) => i >= 0)

  const legend = series.length > 1

  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Time series chart"
      >
        {yTicks.map((tick) => (
          <g key={tick}>
            <line
              x1={PAD_LEFT}
              x2={width - PAD_RIGHT}
              y1={yAt(tick)}
              y2={yAt(tick)}
              className="chart__grid"
            />
            <text
              x={PAD_LEFT - 6}
              y={yAt(tick) + 3}
              textAnchor="end"
              className="chart__tick"
            >
              {formatValue(tick)}
            </text>
          </g>
        ))}
        {xLabelIndexes.map((index) => (
          <text
            key={index}
            x={xAt(index)}
            y={height - 8}
            textAnchor={index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle'}
            className="chart__tick"
          >
            {labels[index]}
          </text>
        ))}
        {series.map((s, sIndex) => (
          <g key={s.name}>
            {paths[sIndex].map((d, pathIndex) => (
              <path key={pathIndex} d={d} className="chart__line" style={{ stroke: s.color }} />
            ))}
            {s.points.map((point, index) =>
              point.value === null ? null : (
                <circle
                  key={index}
                  cx={xAt(index)}
                  cy={yAt(point.value)}
                  r={2.5}
                  fill={s.color}
                >
                  <title>{`${s.name} · ${point.label}: ${formatValue(point.value)}`}</title>
                </circle>
              ),
            )}
          </g>
        ))}
      </svg>
      {legend ? (
        <div className="chart__legend">
          {series.map((s) => (
            <span key={s.name} className="chart__legend-item">
              <span
                className="chart__swatch"
                style={{ background: s.color }}
                aria-hidden="true"
              />
              {s.name}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  )
}
