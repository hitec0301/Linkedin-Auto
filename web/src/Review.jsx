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

export default function Review() {
  const rows = useAsync(() => api.rows(ORDER.join(',')), [])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [fetchMsg, setFetchMsg] = useState('')
  const [picked, setPicked] = useState(() => new Set())

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

      <div className="toprow">
        <p>
          {sorted.length === 0
            ? 'Nothing waiting. Fetch candidates whenever you want the next batch.'
            : drafted > 0
              ? `${drafted} draft${drafted === 1 ? '' : 's'} waiting on you.`
              : 'No drafts waiting. Write a take on a candidate and redraft it.'}
        </p>
        <button className="action" disabled={!!busy} onClick={fetchNow}>
          {busy === 'fetching' ? 'Fetching…' : 'Fetch candidates now'}
        </button>
      </div>
      {fetchMsg && <p className="muted" style={{ marginTop: -10 }}>{fetchMsg}</p>}

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
        />
      ))}
    </>
  )
}
