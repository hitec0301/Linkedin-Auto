import { useState } from 'react'
import { api } from './api.js'
import { Notice, useAsync } from './bits.jsx'
import RowCard from './RowCard.jsx'

// The order the human works in: things waiting on them first, things waiting
// on the machine last. A row they cannot act on should never be at the top of
// the page competing for attention. Terminal statuses (Posted, Skipped,
// Expired) live on Published instead - once a row is done, it stops
// competing with the ones still waiting on a decision.
const ORDER = ['DRAFTED', 'REVISE', 'APPROVED', 'NEW', 'FAILED', 'POSTING']

export default function Review({ onDiscuss }) {
  const rows = useAsync(() => api.rows(ORDER.join(',')), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [fetchMsg, setFetchMsg] = useState('')
  const [picked, setPicked] = useState(() => new Set())
  const [composing, setComposing] = useState(false)
  const [postNotice, setPostNotice] = useState(null)

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

  // Publishing takes the row off this screen the moment it lands (Posted
  // rows live on Published, not Review) - a plain `act()` reload would make
  // a successful post look like the button did nothing. This captures the
  // outcome from the response itself, before that reload can erase it.
  async function postNow(row) {
    setBusy('working')
    setError('')
    setPostNotice(null)
    try {
      const updated = await api.publishNow(row.id)
      if (updated.status === 'POSTED') {
        const link = updated.post_urn
          ? `https://www.linkedin.com/feed/update/${encodeURIComponent(updated.post_urn)}/`
          : ''
        setPostNotice({ kind: 'ok', text: 'Published to LinkedIn.', link })
      } else if (/^dry run/.test(updated.error || '')) {
        setPostNotice({ kind: 'warn', text: `Not published — ${updated.error}` })
      } else if (updated.error) {
        setPostNotice({ kind: 'error', text: updated.error })
      }
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

  function toggle(id) {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function removeSelected() {
    const ids = [...picked]
    if (ids.length === 0) return
    await act(() => api.bulkSkip(ids))
    setPicked(new Set())
  }

  if (rows.loading) return <p className="empty">Loading…</p>
  if (rows.error) return <Notice kind="error">{rows.error}</Notice>

  const sorted = [...(rows.data || [])].sort(
    (a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status),
  )
  const drafted = sorted.filter((r) => r.status === 'DRAFTED').length
  const visibleIds = new Set(sorted.map((r) => r.id))
  const pickedCount = [...picked].filter((id) => visibleIds.has(id)).length

  return (
    <>
      {error && <Notice kind="error">{error}</Notice>}
      {postNotice && (
        <Notice kind={postNotice.kind}>
          {postNotice.text}
          {postNotice.link && (
            <>
              {' '}
              <a href={postNotice.link} target="_blank" rel="noreferrer">View it on LinkedIn ↗</a>
            </>
          )}
          {' '}
          <a href="#" onClick={(e) => { e.preventDefault(); setPostNotice(null) }}>Dismiss</a>
        </Notice>
      )}

      <div className="toprow">
        <p>
          {sorted.length === 0
            ? 'Nothing waiting. Fetch candidates whenever you want the next batch.'
            : drafted > 0
              ? `${drafted} draft${drafted === 1 ? '' : 's'} waiting on you.`
              : 'No drafts waiting. Write a take on a candidate and redraft it.'}
        </p>
        <div className="actions" style={{ marginTop: 0 }}>
          <button className="action" disabled={!!busy} onClick={() => setComposing((v) => !v)}>
            {composing ? 'Cancel' : 'New post'}
          </button>
          <button className="action" disabled={!!busy} onClick={fetchNow}>
            {busy === 'fetching' ? 'Fetching…' : 'Fetch candidates now'}
          </button>
        </div>
      </div>
      {fetchMsg && <p className="muted" style={{ marginTop: -10 }}>{fetchMsg}</p>}

      {composing && (
        <NewPostForm
          onCancel={() => setComposing(false)}
          onCreated={() => { setComposing(false); rows.reload() }}
        />
      )}

      {pickedCount > 0 && (
        <div className="bulkbar">
          <span className="count">{pickedCount} selected</span>
          <div className="right">
            <a href="#" className="clear" onClick={(e) => { e.preventDefault(); setPicked(new Set()) }}>
              Clear selection
            </a>
            <button className="action danger" disabled={!!busy} onClick={removeSelected}>
              Remove selected
            </button>
          </div>
        </div>
      )}

      {sorted.map((row) => (
        <RowCard
          key={row.id}
          row={row}
          busy={busy}
          act={act}
          reload={rows.reload}
          selectable
          selected={picked.has(row.id)}
          onToggleSelect={toggle}
          onDiscuss={onDiscuss}
          onPostNow={postNow}
        />
      ))}
    </>
  )
}

/**
 * Start a post from your own writeup instead of a fetched candidate.
 *
 * Owns its own submit state rather than routing through the page's `act` -
 * a validation error (a malformed URL, most often) should leave the form
 * open with what was typed still in it, not close it and lose the draft.
 */
function NewPostForm({ onCancel, onCreated }) {
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceTitle, setSourceTitle] = useState('')
  const [take, setTake] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setSubmitting(true)
    setError('')
    try {
      await api.createRow({ source_url: sourceUrl, source_title: sourceTitle, take })
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="panel card">
      <h3>New post</h3>
      {error && <Notice kind="error">{error}</Notice>}

      <label className="field-label">Source URL — the article you're citing</label>
      <input
        type="url"
        value={sourceUrl}
        placeholder="https://…"
        onChange={(e) => setSourceUrl(e.target.value)}
      />

      <label className="field-label">Title, if you want one (optional)</label>
      <input
        type="text"
        value={sourceTitle}
        placeholder="Untitled if left blank"
        onChange={(e) => setSourceTitle(e.target.value)}
      />

      <label className="field-label">Your writeup — the thesis a first draft is built from</label>
      <textarea
        className="note"
        rows={5}
        value={take}
        placeholder="What's your take on this article? Write as much as you want - this is what the draft argues, the article is just evidence for it."
        onChange={(e) => setTake(e.target.value)}
      />

      <div className="actions">
        <button
          className="action primary"
          disabled={submitting || !sourceUrl.trim()}
          onClick={submit}
        >
          {submitting ? 'Creating…' : 'Create'}
        </button>
        <button className="action" disabled={submitting} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}
