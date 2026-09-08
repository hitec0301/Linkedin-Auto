import { useState } from 'react'
import { api } from './api.js'
import { Notice, useAsync } from './bits.jsx'
import RowCard from './RowCard.jsx'

export default function Published() {
  const rows = useAsync(() => api.rows('POSTED,EXPIRED,SKIPPED,FAILED'), [])
  const health = useAsync(() => api.health(), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [restored, setRestored] = useState(null)

  async function act(fn) {
    setBusy('working')
    setError('')
    try {
      await fn()
      rows.reload()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  // Restoring moves the row to Approved, which takes it off this list on
  // the very next reload - so, same as "Post now", the confirmation has to
  // come from the response itself, before that reload can erase all trace
  // of what just happened.
  async function restore(row) {
    setBusy('working')
    setError('')
    setRestored(null)
    try {
      const updated = await api.restoreRow(row.id)
      setRestored({
        title: row.source_title || 'Untitled',
        when: (updated.scheduled_for || '').replace('T', ' ').replace('Z', ''),
      })
      rows.reload()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  if (rows.loading) return <p className="empty">Loading…</p>
  if (rows.error) return <Notice kind="error">{rows.error}</Notice>

  const posted = (rows.data || []).filter((r) => r.status === 'POSTED')
  const rest = (rows.data || []).filter((r) => r.status !== 'POSTED')
  const h = health.data

  return (
    <>
      {error && <Notice kind="error">{error}</Notice>}
      {restored && (
        <Notice kind="ok">
          "{restored.title}" is back on Review, Approved
          {restored.when ? ` and due ${restored.when}` : ''}.
          {' '}
          <a href="#" onClick={(e) => { e.preventDefault(); setRestored(null) }}>Dismiss</a>
        </Notice>
      )}
      {h && <Health stats={h} />}

      {posted.length === 0 && <p className="empty">Nothing published yet.</p>}

      {posted.map((row) => (
        <RowCard key={row.id} row={row} busy={busy} act={act} reload={rows.reload} />
      ))}

      {rest.length > 0 && (
        <div className="panel">
          <h3>Not published</h3>
          <table>
            <thead>
              <tr><th>Status</th><th>Item</th><th>Why</th><th /></tr>
            </thead>
            <tbody>
              {rest.map((row) => (
                <tr key={row.id}>
                  <td>{row.status.toLowerCase()}</td>
                  <td>{row.source_title}</td>
                  <td className="muted">{row.error || '—'}</td>
                  <td>
                    {(row.status === 'FAILED' || row.status === 'EXPIRED') && (
                      <button
                        className="action"
                        disabled={!!busy}
                        onClick={() => restore(row)}
                      >
                        {busy === 'working' ? 'Republishing…' : 'Republish'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/**
 * The measurement that decides whether this is worth paying for.
 *
 * Shown to the customer, verdict and all. If the tool costs more editing time
 * than it saves, the person paying for it is the one who most needs to know.
 */
function Health({ stats }) {
  const bad = stats.verdict.includes('shut it off')
  return (
    <div className="panel">
      <div className="stat-grid">
        <div className="stat"><b>{stats.published}</b><span>published</span></div>
        <div className="stat">
          <b>{Math.round(stats.clean_rate * 100)}%</b>
          <span>published with no edits</span>
        </div>
        <div className="stat">
          <b>{Math.round(stats.mean_edit_distance_last * 100)}%</b>
          <span>you rewrite, recently</span>
        </div>
        <div className="stat">
          <b>{stats.improving === null ? '—' : stats.improving ? 'yes' : 'no'}</b>
          <span>getting closer to your voice</span>
        </div>
      </div>
      <p className={bad ? 'notice error' : 'muted'} style={{ marginBottom: 0 }}>
        {stats.verdict}
      </p>
    </div>
  )
}
