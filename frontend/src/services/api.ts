/**
 * Centralized backend access for the LLMCacheX dashboard.
 *
 * All dashboard data comes from the local FastAPI backend — there are no
 * hardcoded statistics anywhere in the UI. The base URL comes from
 * VITE_API_BASE_URL (see frontend/.env).
 */

import type {
  ActivityResponse,
  AnalyticsSummary,
  CacheListResponse,
  ClearAnalyticsResponse,
  ClearCacheResponse,
  HealthResponse,
  ProviderAnalytics,
  StatsResponse,
  TimeseriesResponse,
  UsageAnalytics,
} from '../types/api'

const BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '')

export class ApiError extends Error {
  readonly status?: number

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...init?.headers,
      },
    })
  } catch {
    // Network-level failure: backend not running / unreachable.
    throw new ApiError('Unable to connect to the LLMCacheX backend.')
  }
  if (!response.ok) {
    throw new ApiError(
      `Backend request failed with status ${response.status}.`,
      response.status,
    )
  }
  return (await response.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}

export function getStats(): Promise<StatsResponse> {
  return request<StatsResponse>('/api/stats')
}

export function getCacheEntries(): Promise<CacheListResponse> {
  return request<CacheListResponse>('/api/cache')
}

export function getActivity(limit = 100): Promise<ActivityResponse> {
  return request<ActivityResponse>(`/api/activity?limit=${limit}`)
}

export function clearCache(): Promise<ClearCacheResponse> {
  return request<ClearCacheResponse>('/api/cache', { method: 'DELETE' })
}

export function getAnalyticsSummary(): Promise<AnalyticsSummary> {
  return request<AnalyticsSummary>('/api/analytics/summary')
}

export function getAnalyticsTimeseries(
  bucket: 'hour' | 'day' = 'hour',
  hours = 24,
): Promise<TimeseriesResponse> {
  return request<TimeseriesResponse>(
    `/api/analytics/timeseries?bucket=${bucket}&hours=${hours}`,
  )
}

export function getProviderAnalytics(): Promise<ProviderAnalytics[]> {
  return request<ProviderAnalytics[]>('/api/analytics/providers')
}

export function getUsageAnalytics(): Promise<UsageAnalytics> {
  return request<UsageAnalytics>('/api/analytics/usage')
}

export function clearAnalytics(): Promise<ClearAnalyticsResponse> {
  return request<ClearAnalyticsResponse>('/api/analytics', {
    method: 'DELETE',
  })
}

/** The backend base URL, for display in the UI (not a secret). */
export const apiBaseUrl = BASE_URL
