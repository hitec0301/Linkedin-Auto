import { api } from './api.js'
import { AutoSave, Notice, useAsync } from './bits.jsx'

export default function Published() {
  const rows = useAsync(() => api.rows('POSTED,EXPIRED,SKIPPED,FAILED'), [])
  const health = useAsync(() => api.health(), [])

  if (rows.loading) return <p className="empty">Loading…</p>
  if (rows.error) return <Notice kind="error">{rows.error}</Notice>

  const posted = (rows.data || []).filter((r) => r.status === 'POSTED')
  const rest = (rows.data || []).filter((r) => r.status !== 'POSTED')
  const h = health.data

  return (
    <>
      {h && <Health stats={h} />}

      {posted.length === 0 && <p className="empty">Nothing published yet.</p>}

      {posted.map((row) => (
        <div className="panel" key={row.id}>
          <h3>{row.source_title || 'Untitled'}</h3>
          <div className="meta">
            <span>{(row.posted_at || '').replace('T', ' ').replace('Z', '')}</span>
            {row.edit_distance > 0 && <span>you changed {Math.round(row.edit_distance * 100)}%</span>}
            {row.revision_count > 0 && <span>{row.revision_count} revision(s)</span>}
          </div>
          <div className="post-text">{row.final_text || row.draft_text}</div>
          <label>
            Impressions, when you have them. Nothing reads this automatically —
            LinkedIn's analytics are not scraped.
          </label>
          <AutoSave
            id={row.id}
            rows={1}
            value={row.reach ? String(row.reach) : ''}
            placeholder="e.g. 4200"
            onSave={(v) => api.editRow(row.id, { reach: Number(v.replace(/\D/g, '')) || 0 })}
          />
        </div>
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
