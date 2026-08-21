import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Notice, useAsync } from './bits.jsx'

export default function Voice() {
  const card = useAsync(() => api.voiceCard().catch(() => ({ content: '' })), [])
  const amendments = useAsync(() => api.amendments(), [])
  const [text, setText] = useState('')
  const [state, setState] = useState('')

  useEffect(() => { if (card.data) setText(card.data.content) }, [card.data])

  async function save() {
    setState('saving')
    try {
      await api.saveVoiceCard(text)
      setState('saved')
      card.reload()
    } catch (err) {
      setState(err.message)
    }
  }

  const pending = (amendments.data || []).filter((a) => !a.applied)
  const applied = (amendments.data || []).filter((a) => a.applied)

  return (
    <>
      <div className="panel">
        <h3>Your voice card</h3>
        <p className="muted">
          This is the instruction the drafts are written from. It is plain text
          and you can change any of it — when a draft comes out wrong, fixing a
          sentence here is the repair, and it takes effect on the next draft.
        </p>
        <textarea
          className="card-editor"
          value={text}
          onChange={(e) => { setText(e.target.value); setState('') }}
        />
        <div className="actions">
          <button className="action primary" onClick={save} disabled={!text.trim()}>
            Save the card
          </button>
          <span className="muted">{state}</span>
        </div>
      </div>

      <div className="panel">
        <h3>Proposed rules</h3>
        <p className="muted">
          Written from your corrections each Sunday. Nothing here reaches the
          card until you tick it, and ticking it queues it for the next weekly
          run rather than editing the card behind you.
        </p>

        {pending.length === 0 && <p className="empty">No proposals waiting.</p>}

        {pending.map((a) => (
          <div key={a.id} style={{ borderTop: '1px solid var(--line)', paddingTop: 14, marginTop: 14 }}>
            <label className="switch">
              <input
                type="checkbox"
                checked={a.accepted}
                onChange={(e) => api.decideAmendment(a.id, e.target.checked).then(amendments.reload)}
              />
              <b>{a.rule}</b>
            </label>
            <p className="muted" style={{ margin: '6px 0 0 26px' }}>
              {a.rationale}
              {a.recurring && (
                <>
                  {' '}<strong>You have made this correction {a.occurrences} times.</strong>
                </>
              )}
            </p>
          </div>
        ))}

        {applied.length > 0 && (
          <>
            <h4 className="muted" style={{ marginBottom: 4 }}>Already in the card</h4>
            <ul className="muted">
              {applied.map((a) => <li key={a.id}>{a.rule}</li>)}
            </ul>
          </>
        )}
      </div>

      {amendments.error && <Notice kind="error">{amendments.error}</Notice>}
    </>
  )
}
