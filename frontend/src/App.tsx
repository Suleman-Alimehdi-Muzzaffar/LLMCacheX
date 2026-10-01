import Analytics from './pages/Analytics'
import Dashboard from './pages/Dashboard'
import useHashRoute from './hooks/useHashRoute'
import './App.css'

function App() {
  const route = useHashRoute()
  const onAnalytics = route.startsWith('/analytics')

  return (
    <div className="app-shell">
      <header className="app-nav">
        <div className="topbar__brand">
          <span className="topbar__mark" aria-hidden="true">
            ▲
          </span>
          <div>
            <h1>LLMCacheX</h1>
            <p className="topbar__subtitle">Developer Dashboard</p>
          </div>
        </div>
        <nav className="app-nav__links" aria-label="Primary">
          <a
            href="#/"
            className={`app-nav__link ${onAnalytics ? '' : 'app-nav__link--active'}`}
          >
            Dashboard
          </a>
          <a
            href="#/analytics"
            className={`app-nav__link ${onAnalytics ? 'app-nav__link--active' : ''}`}
          >
            Analytics
          </a>
        </nav>
      </header>

      {onAnalytics ? <Analytics /> : <Dashboard />}

      <footer className="app-footer">
        LLMCacheX · local developer dashboard · no external API keys required
      </footer>
    </div>
  )
}

export default App
