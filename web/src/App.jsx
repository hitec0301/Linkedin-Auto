import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import Review from './Review.jsx'
import Published from './Published.jsx'
import Voice from './Voice.jsx'
import Sources from './Sources.jsx'
import Setup from './Setup.jsx'
import SignIn from './SignIn.jsx'
import { Notice } from './bits.jsx'

// Routing is a string. There are five screens and no nested state, so a
// router would be a dependency bought to solve a problem this app does not
// have. The path is kept in sync so a reload and the back button both work.
const TABS = [
  ['review', 'Review'],
  ['published', 'Published'],
  ['voice', 'Voice'],
  ['sources', 'Sources'],
  ['setup', 'Setup'],
]

function tabFromPath() {
  const name = window.location.pathname.replace(/^\//, '')
  return TABS.some(([id]) => id === name) ? name : 'review'
}

export default function App() {
  const [me, setMe] = useState(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState(tabFromPath)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me())
      setError('')
    } catch (err) {
      if (err.status === 401) setMe(null)
      else setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    const onPop = () => setTab(tabFromPath())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  function go(next) {
    setTab(next)
    window.history.pushState({}, '', `/${next}`)
  }

  if (loading) return <div className="shell"><p className="empty">Loading…</p></div>
  if (!me) return <SignIn />

  // Setup is not optional and not skippable: without a connected LinkedIn
  // account there is nothing this product can do, so an unfinished account
  // lands there rather than on an empty review screen it cannot explain.
  const needsSetup = !me.linkedin_connected
  const active = needsSetup && tab !== 'setup' ? 'setup' : tab

  return (
    <div className="shell">
      <header className="top">
        <h1>Publishing pipeline</h1>
        <div className="spacer" />
        <nav>
          {TABS.map(([id, label]) => (
            <button
              key={id}
              className={active === id ? 'on' : ''}
              onClick={() => go(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <button
          className="action"
          onClick={async () => { await api.signOut(); setMe(null) }}
        >
          Sign out
        </button>
      </header>

      {error && <Notice kind="error">{error}</Notice>}

      {needsSetup && active === 'setup' && (
        <Notice>
          Nothing can be drafted or published until LinkedIn is connected.
          These are the remaining steps.
        </Notice>
      )}

      {me.paused && !needsSetup && (
        <Notice>
          Publishing is paused, so nothing will go out. Drafting continues.
          {' '}
          <a href="#" onClick={(e) => { e.preventDefault(); go('setup') }}>
            Turn it back on in Setup.
          </a>
        </Notice>
      )}

      {active === 'review' && <Review me={me} />}
      {active === 'published' && <Published />}
      {active === 'voice' && <Voice />}
      {active === 'sources' && <Sources me={me} go={go} />}
      {active === 'setup' && <Setup me={me} onChange={refresh} go={go} />}
    </div>
  )
}
