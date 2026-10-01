/** Types mirroring the FastAPI response schemas (backend/api/schemas.py). */

export interface HealthResponse {
  status: string
  service: string
  cache_backend: string
}

export interface StatsResponse {
  total_requests: number
  cache_hits: number
  cache_misses: number
  semantic_hits?: number
  hit_rate: number
  stored_entries: number
  expired_entries: number
  retry_count: number
  rate_limit_events: number
  successful_calls: number
  failed_calls: number
  avg_execution_latency_ms: number
  latest_execution_latency_ms: number
  total_execution_time_ms: number
}

export interface CacheEntry {
  request_hash: string
  created_at: string
  expires_at: string | null
  expired: boolean
}

export interface CacheListResponse {
  entries: CacheEntry[]
  count: number
}

export interface ActivityEvent {
  type: string
  function: string
  timestamp: string
  latency_ms: number | null
}

export interface ActivityResponse {
  events: ActivityEvent[]
  count: number
}

export interface ClearCacheResponse {
  message: string
  cleared: boolean
}

/** GET /api/analytics/summary — null means unknown, not zero. */
export interface AnalyticsSummary {
  total_requests: number
  cache_hits: number
  cache_misses: number
  semantic_hits?: number
  hit_rate: number
  successful_requests: number
  failed_requests: number
  retry_count: number
  rate_limit_events: number
  total_execution_time_ms: number
  average_latency_ms: number | null
  total_tokens: number | null
  estimated_cost: number | null
  estimated_cost_saved: number | null
}

export interface TimeseriesPoint {
  timestamp: string
  requests: number
  cache_hits: number
  cache_misses: number
  average_latency_ms: number | null
  estimated_cost: number | null
}

export interface TimeseriesResponse {
  bucket: 'hour' | 'day'
  hours: number
  points: TimeseriesPoint[]
  count: number
}

export interface ProviderAnalytics {
  provider: string
  requests: number
  cache_hits: number
  cache_misses: number
  total_tokens: number | null
  estimated_cost: number | null
}

export interface UsageAnalytics {
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  request_units: number | null
  estimated_cost: number | null
  estimated_cost_saved: number | null
}

export interface ClearAnalyticsResponse {
  message: string
  cleared: boolean
  events_cleared: number
}
