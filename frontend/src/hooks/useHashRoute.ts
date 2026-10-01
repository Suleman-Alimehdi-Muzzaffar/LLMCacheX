/**
 * Minimal hash-based routing for the dashboard.
 *
 * The project has no router dependency; hash routes (`#/`,
 * `#/analytics`) work with the plain Vite dev server without any
 * server-side rewrite configuration.
 */

import { useEffect, useState } from 'react'

function readRoute(): string {
  const hash = window.location.hash
  return hash.startsWith('#') ? hash.slice(1) : ''
}

export default function useHashRoute(): string {
  const [route, setRoute] = useState<string>(readRoute)

  useEffect(() => {
    const onChange = () => setRoute(readRoute())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  return route
}
