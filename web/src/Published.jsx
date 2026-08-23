import { useState } from 'react'
import { api } from './api.js'
import { Notice, useAsync } from './bits.jsx'
import RowCard from './RowCard.jsx'

export default function Published() {
  const rows = useAsync(() => api.rows('POSTED,EXPIRED,SKIPPED,FAILED'), [])
  const health = useAsync(() => api.health(), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

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

  if (rows.loading) return <p className="empty">Loading…</p>
  if (rows.error) return <Notice kind="error">{rows.error}</Notice>

  const posted = (rows.data || []).filter((r) => r.status === 'POSTED')
  const rest = (rows.data || []).filter((r) => r.status !== 'POSTED')
  const h = health.data

  return (
    <>
      {error && <Notice kind="error">{error}</Notice>}
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
              <tr><th>Status</th><th>Item</th><th>Why</th></tr>
            </thead>
            <tbody>
              {rest.map((row) => (
                <tr key={row.id}>
                  <td>{row.status.toLowerCase()}</td>
                  <td>{row.source_title}</td>
                  <td className="muted">{row.error || '—'}</td>
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
