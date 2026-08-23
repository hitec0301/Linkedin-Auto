import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { Notice } from './bits.jsx'

// Every real status except POSTING, which is a marker the publish path sets
// on itself while a post is going out - not something a person chooses.
const STATUS_OPTIONS = ['NEW', 'DRAFTED', 'REVISE', 'APPROVED', 'FAILED', 'SKIPPED', 'EXPIRED', 'POSTED']

function label(status) {
  return status.charAt(0) + status.slice(1).toLowerCase()
}

/**
 * One card, for the row's whole life: candidate, draft, approved, posted -
 * same template throughout. "Your take" is the angle before a draft exists
 * and the revision instruction once one does; "Redraft with AI" always reads
 * whatever is in that box right now, not a debounced autosave of it, because
 * the field and the button sit right next to each other and a click should
 * carry exactly what is on screen.
 */
export default function RowCard({ row, busy, act, reload, selectable, selected, onToggleSelect, onDiscuss }) {
  const [take, setTake] = useState(row.take || '')
  const [saveState, setSaveState] = useState('idle')
  const [scheduledAt, setScheduledAt] = useState(toLocalInput(row.scheduled_for))
  const timer = useRef(null)

  useEffect(() => {
    setTake(row.take || '')
    setSaveState('idle')
  }, [row.id, row.take])

  useEffect(() => {
    setScheduledAt(toLocalInput(row.scheduled_for))
  }, [row.id, row.scheduled_for])

  function changeTake(next) {
    setTake(next)
    setSaveState('editing')
    clearTimeout(timer.current)
    timer.current = setTimeout(async () => {
      setSaveState('saving')
      try {
        await api.editRow(row.id, { take: next })
        setSaveState('saved')
      } catch (err) {
        setSaveState(`error: ${err.message}`)
      }
    }, 700)
  }

  async function redraft() {
    clearTimeout(timer.current)
    await act(() => api.redraftNow(row.id, take))
  }

  const text = row.final_text || row.draft_text
  const hasDraft = !!row.draft_text
  const isTerminal = ['POSTED', 'SKIPPED', 'EXPIRED'].includes(row.status)

  return (
    <div className={`panel card${selected ? ' picked' : ''}`}>
      <div className="card-head">
        {selectable && (
          <input
            className="pick"
            type="checkbox"
            checked={!!selected}
            onChange={() => onToggleSelect(row.id)}
          />
        )}
        <h3>{row.source_title || 'Untitled'}</h3>
        <select
          className={`status st-${row.status}`}
          value={row.status}
          disabled={row.status === 'POSTING' || !!busy}
          onChange={(e) => act(() => api.setStatus(row.id, e.target.value))}
        >
          {STATUS_OPTIONS.includes(row.status) ? null : (
            <option value={row.status}>{label(row.status)}</option>
          )}
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{label(s)}</option>
          ))}
        </select>
      </div>

      <div className="card-body">
        <div className="meta">
          {row.audience && <span className="tag">{row.audience.replace('AUD_', '').toLowerCase()}</span>}
          {row.theme && <span className="tag">{row.theme.replace('THM_', '').toLowerCase()}</span>}
          {row.source_url && <a href={row.source_url} target="_blank" rel="noreferrer">source ↗</a>}
        </div>
        {row.why_it_matters && <p className="why">{row.why_it_matters}</p>}

        {row.error && <Notice kind="error">{row.error}</Notice>}

        {row.status === 'POSTING' && <p className="muted">Publishing now — nothing to do.</p>}

        {text && (
          <>
            <div className="post-text">{text}</div>
            {row.status === 'POSTED' ? (
              <p className="posted-meta">
                {(row.posted_at || '').replace('T', ' ').replace('Z', '')}
                {row.edit_distance > 0 && ` · you changed ${Math.round(row.edit_distance * 100)}%`}
                {row.revision_count > 0 && ` · ${row.revision_count} revision(s)`}
              </p>
            ) : (
              <p className="charcount">{text.length} characters{row.final_text && row.final_text !== row.draft_text && ' · edited by you'}</p>
            )}
          </>
        )}

        {hasDraft && !isTerminal && (
          <>
            <label className="field-label">Your version — edit freely. The draft above is kept as written.</label>
            <textarea
              className="note"
              rows={4}
              value={row.final_text}
              placeholder="Leave empty to publish the draft as it stands."
              onChange={(e) => api.editRow(row.id, { final_text: e.target.value })}
              onBlur={(e) => act(() => api.editRow(row.id, { final_text: e.target.value }))}
            />
          </>
        )}

        {row.status === 'APPROVED' && (
          <div className="schedule-row">
            <label>Publish</label>
            <input
              type="datetime-local"
              value={scheduledAt}
              onChange={(e) => {
                setScheduledAt(e.target.value)
                if (e.target.value) {
                  act(() => api.reschedule(row.id, new Date(e.target.value).toISOString()))
                }
              }}
            />
            <span className="muted">or</span>
            <button className="action" disabled={!!busy} onClick={() => act(() => api.publishNow(row.id))}>
              {busy === 'working' ? 'Posting…' : 'Post now'}
            </button>
          </div>
        )}

        {row.status === 'POSTED' && (
          <>
            <label className="field-label">
              Impressions, when you have them. Nothing reads this automatically.
            </label>
            <input
              type="text"
              value={row.reach ? String(row.reach) : ''}
              placeholder="e.g. 4200"
              onChange={(e) => api.editRow(row.id, { reach: Number(e.target.value.replace(/\D/g, '')) || 0 })}
              onBlur={reload}
            />
          </>
        )}

        <label className="field-label">Your take</label>
        <textarea
          className="note"
          rows={2}
          value={take}
          placeholder={
            hasDraft
              ? 'What should change? Write it as a rule, not a rewrite.'
              : 'e.g. the compliance-training numbers everyone quotes are measuring the wrong thing'
          }
          onChange={(e) => changeTake(e.target.value)}
        />
        <div className="note-row">
          <div className="field muted right">
            {saveState === 'saving' && 'saving…'}
            {saveState === 'saved' && 'saved'}
            {saveState.startsWith?.('error') && saveState}
          </div>
          <button className="action primary" disabled={!!busy} onClick={redraft}>
            {busy === 'working' ? 'Working…' : 'Redraft with AI'}
          </button>
        </div>
        {isTerminal && (
          <p className="posted-note">
            {row.status === 'POSTED'
              ? 'Redrafting a posted item starts a fresh draft below it — it will not edit or repost this one.'
              : 'Redrafting a retired item starts a fresh draft below it — it will not revive this one.'}
          </p>
        )}
        {!hasDraft && !take.trim() && !isTerminal && (
          <p className="redraft-hint">
            No take yet — redrafting still writes a first version, generic until you edit it.
            {onDiscuss && (
              <>
                {' '}Or{' '}
                <a
                  href="#"
                  onClick={(e) => {
                    e.preventDefault()
                    onDiscuss({
                      source_url: row.source_url,
                      source_title: row.source_title,
                      row_id: row.id,
                    })
                  }}
                >
                  discuss it first
                </a>.
              </>
            )}
          </p>
        )}
      </div>
    </div>
  )
}

function toLocalInput(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
