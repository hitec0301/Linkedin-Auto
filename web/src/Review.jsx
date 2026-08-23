import { useState } from 'react'
import { api } from './api.js'
import { AutoSave, Confirm, Notice, StatusTag, useAsync } from './bits.jsx'

// The order the human works in: things waiting on them first, things waiting
// on the machine last. A row they cannot act on should never be at the top of
// the page competing for attention.
const ORDER = ['DRAFTED', 'REVISE', 'APPROVED', 'NEW', 'FAILED', 'POSTING']

export default function Review() {
  const rows = useAsync(() => api.rows(ORDER.join(',')), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [fetchMsg, setFetchMsg] = useState('')

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

  async function fetchNow() {
    setBusy('fetching')
    setError('')
    setFetchMsg('')
    try {
      const result = await api.curateNow()
      setFetchMsg(result.detail)
      rows.reload()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  if (rows.loading) return <p className="empty">Loading…</p>
  if (rows.error) return <Notice kind="error">{rows.error}</Notice>

  const sorted = [...(rows.data || [])].sort(
    (a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status),
  )
  const drafted = sorted.filter((r) => r.status === 'DRAFTED').length

  return (
    <>
      {error && <Notice kind="error">{error}</Notice>}

      <div className="switch" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
        <p className="muted" style={{ margin: 0 }}>
          {sorted.length === 0
            ? 'Nothing waiting. The next batch of candidates arrives Monday morning.'
            : drafted > 0
              ? `${drafted} draft${drafted === 1 ? '' : 's'} waiting on you.`
              : 'No drafts waiting. Tick the candidates you want and give each an angle.'}
        </p>
        <button className="action" disabled={!!busy} onClick={fetchNow}>
          {busy === 'fetching' ? 'Fetching…' : 'Fetch more candidates'}
        </button>
      </div>
      {fetchMsg && <p className="muted" style={{ marginTop: -10 }}>{fetchMsg}</p>}

      {sorted.map((row) => (
        <RowCard key={row.id} row={row} busy={busy} act={act} reload={rows.reload} />
      ))}
    </>
  )
}

function RowCard({ row, busy, act, reload }) {
  const [revising, setRevising] = useState(false)
  const [note, setNote] = useState('')
  const [scheduling, setScheduling] = useState(false)
  const [scheduledAt, setScheduledAt] = useState('')
  const can = (name) => row.allowed_actions.includes(name)
  const text = row.final_text || row.draft_text
  const isApproved = row.status === 'APPROVED'

  return (
    <div className="panel row-card">
      <h3>{row.source_title || 'Untitled'}</h3>
      <div className="meta">
        <StatusTag status={row.status} />
        {row.audience && <span className="tag">{row.audience.replace('AUD_', '').toLowerCase()}</span>}
        {row.theme && <span className="tag">{row.theme.replace('THM_', '').toLowerCase()}</span>}
        {row.source_url && <a href={row.source_url} target="_blank" rel="noreferrer">source</a>}
        {row.scheduled_for && <span>slot {row.scheduled_for.replace('T', ' ').replace('Z', '')}</span>}
        {row.revision_count > 0 && <span>{row.revision_count} revision(s)</span>}
      </div>

      {row.why_it_matters && <p className="muted">{row.why_it_matters}</p>}

      {row.error && <Notice kind="error">{row.error}</Notice>}

      {row.status === 'NEW' && (
        <>
          <label className="switch">
            <input
              type="checkbox"
              checked={row.selected}
              onChange={(e) => act(() => api.editRow(row.id, { selected: e.target.checked }))}
            />
            Write about this one
          </label>
          <label htmlFor={`angle-${row.id}`}>
            Angle — one line, in your words. This is what the draft is built from.
          </label>
          <AutoSave
            id={row.id}
            value={row.angle}
            placeholder="e.g. the compliance-training numbers everyone quotes are measuring the wrong thing"
            onSave={(v) => api.editRow(row.id, { angle: v }).then(reload)}
          />
          {row.selected && row.angle?.trim() && (
            <div className="actions">
              <button
                className="action primary"
                disabled={!!busy}
                onClick={() => act(() => api.redraftNow(row.id))}
              >
                {busy === 'working' ? 'Drafting…' : 'Draft now'}
              </button>
            </div>
          )}
        </>
      )}

      {text && (
        <>
          <div className="post-text">{text}</div>
          <div className="muted">
            {text.length} characters
            {row.final_text && row.final_text !== row.draft_text && ' · edited by you'}
          </div>
        </>
      )}

      {['DRAFTED', 'REVISE', 'APPROVED'].includes(row.status) && (
        <>
          <label>Your version — edit freely. The draft above is kept as written.</label>
          <AutoSave
            id={row.id}
            rows={8}
            value={row.final_text}
            placeholder="Leave empty to publish the draft as it stands."
            onSave={(v) => api.editRow(row.id, { final_text: v })}
          />
        </>
      )}

      {revising && (
        <>
          <label>What should change? Write it as a rule, not a rewrite.</label>
          <textarea
            rows={3}
            value={note}
            placeholder="e.g. stop opening with a question"
            onChange={(e) => setNote(e.target.value)}
          />
        </>
      )}

      {scheduling && (
        <>
          <label>When should this go out?</label>
          <div className="actions">
            <button
              className="action primary"
              disabled={!!busy}
              onClick={() => act(async () => {
                await (isApproved ? api.publishNow(row.id) : api.approve(row.id, { publish_now: true }))
                setScheduling(false)
              })}
            >
              Post now
            </button>
          </div>
          <p className="muted" style={{ margin: '10px 0 4px' }}>or pick a time</p>
          <input
            type="datetime-local"
            value={scheduledAt}
            onChange={(e) => setScheduledAt(e.target.value)}
          />
        </>
      )}

      <div className="actions">
        {can('approve') && !revising && !scheduling && (
          <button
            className="action primary"
            disabled={!!busy || !text.trim()}
            onClick={() => act(() => api.approve(row.id))}
          >
            Approve for publishing
          </button>
        )}
        {(can('approve') || can('publish_now')) && !revising && !scheduling && (
          <button
            className="action"
            disabled={!!busy || !text.trim()}
            onClick={() => setScheduling(true)}
          >
            Choose when…
          </button>
        )}
        {scheduling && (
          <>
            <button
              className="action primary"
              disabled={!scheduledAt || !!busy}
              onClick={() => act(async () => {
                const when = new Date(scheduledAt).toISOString()
                await (isApproved
                  ? api.reschedule(row.id, when)
                  : api.approve(row.id, { scheduled_for: when }))
                setScheduling(false)
                setScheduledAt('')
              })}
            >
              {isApproved ? 'Reschedule for that time' : 'Approve, scheduled for that time'}
            </button>
            <button className="action" onClick={() => { setScheduling(false); setScheduledAt('') }}>
              Cancel
            </button>
          </>
        )}
        {can('revise') && !revising && !scheduling && (
          <button className="action" onClick={() => setRevising(true)}>
            Send back with a note
          </button>
        )}
        {can('redraft') && (
          <button
            className="action"
            disabled={!!busy}
            onClick={() => act(() => api.redraftNow(row.id))}
          >
            {busy === 'working' ? 'Redrafting…' : 'Redraft now'}
          </button>
        )}
        {revising && (
          <>
            <button
              className="action primary"
              disabled={!note.trim() || !!busy}
              onClick={() => act(async () => {
                await api.revise(row.id, note)
                setRevising(false)
                setNote('')
              })}
            >
              Send back
            </button>
            <button className="action" onClick={() => { setRevising(false); setNote('') }}>
              Cancel
            </button>
          </>
        )}
        {can('unapprove') && !revising && !scheduling && (
          <Confirm
            label="Take approval back"
            question="This retires the row rather than returning it to draft. Sure?"
            onConfirm={() => act(() => api.unapprove(row.id))}
          />
        )}
        {can('skip') && !revising && !scheduling && (
          <Confirm
            label="Skip"
            question="Skipped rows do not come back. Sure?"
            onConfirm={() => act(() => api.skip(row.id))}
          />
        )}
        {row.status === 'POSTING' && (
          <span className="muted">Publishing now — nothing to do.</span>
        )}
      </div>
    </div>
  )
}
