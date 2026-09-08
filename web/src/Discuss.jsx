import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Notice, useAsync } from './bits.jsx'

/**
 * Explore a source, argue with it, before it becomes a post.
 *
 * A discussion is a scratchpad: nothing here is a pipeline row until "Turn
 * into a post" commits it to one. `seed`, when set by a "Discuss" click on
 * a Review card, starts a fresh discussion immediately from that row's
 * source instead of showing the list first.
 */
export default function Discuss({ seed, onSeedConsumed, go }) {
  const discussions = useAsync(() => api.discussions(), [])
  const [openId, setOpenId] = useState(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!seed) return
    onSeedConsumed()
    start(seed)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed])

  async function start(body) {
    setStarting(true)
    setError('')
    try {
      const created = await api.startDiscussion(body)
      discussions.reload()
      setOpenId(created.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setStarting(false)
    }
  }

  if (openId) {
    return (
      <DiscussionChat
        id={openId}
        go={go}
        onBack={() => { setOpenId(null); discussions.reload() }}
        onBecamePost={() => { setOpenId(null); discussions.reload() }}
      />
    )
  }

  if (discussions.loading) return <p className="empty">Loading…</p>

  return (
    <>
      <div className="toprow">
        <p>Explore a source and argue it out before it becomes a post.</p>
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {discussions.error && <Notice kind="error">{discussions.error}</Notice>}

      <NewDiscussionForm busy={starting} onStart={start} />

      {(discussions.data || []).length === 0 ? (
        <p className="empty">No discussions yet.</p>
      ) : (
        discussions.data.map((d) => (
          <button
            key={d.id}
            className="panel card discussion-row"
            onClick={() => setOpenId(d.id)}
          >
            <div className="card-head">
              <h3>{d.source_title || d.source_url}</h3>
              {d.row_id && <span className="tag">became a post</span>}
            </div>
            <div className="card-body">
              <p className="muted">
                {d.messages.length} message{d.messages.length === 1 ? '' : 's'}
                {' · '}
                {(d.updated_at || '').replace('T', ' ').replace('Z', '')}
              </p>
            </div>
          </button>
        ))
      )}
    </>
  )
}

function NewDiscussionForm({ busy, onStart }) {
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceTitle, setSourceTitle] = useState('')
  const [error, setError] = useState('')

  async function submit() {
    setError('')
    try {
      await onStart({ source_url: sourceUrl, source_title: sourceTitle })
      setSourceUrl('')
      setSourceTitle('')
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="panel card">
      <h3>New discussion</h3>
      {error && <Notice kind="error">{error}</Notice>}

      <label className="field-label">Source URL — the article to discuss</label>
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

      <div className="actions">
        <button
          className="action primary"
          disabled={busy || !sourceUrl.trim()}
          onClick={submit}
        >
          {busy ? 'Starting…' : 'Start discussing'}
        </button>
      </div>
    </div>
  )
}

function DiscussionChat({ id, onBack, onBecamePost, go }) {
  const [discussion, setDiscussion] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')

  async function load() {
    setLoading(true)
    try {
      setDiscussion(await api.discussion(id))
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  async function send() {
    if (!text.trim()) return
    setBusy('sending')
    setError('')
    try {
      setDiscussion(await api.postDiscussionMessage(id, text))
      setText('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  async function turnIntoPost() {
    setBusy('posting')
    setError('')
    try {
      await api.turnIntoPost(id)
      onBecamePost()
      go?.('review')
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }

  async function abandon() {
    setBusy('deleting')
    setError('')
    try {
      await api.deleteDiscussion(id)
      onBack()
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }

  if (loading) return <p className="empty">Loading…</p>
  if (!discussion) return <Notice kind="error">{error}</Notice>

  const hasArgument = discussion.messages.some((m) => m.role === 'user')
  const committed = !!discussion.row_id

  return (
    <>
      <div className="toprow">
        <a href="#" onClick={(e) => { e.preventDefault(); onBack() }}>← Back to discussions</a>
      </div>

      {error && <Notice kind="error">{error}</Notice>}

      <div className="panel card">
        <div className="card-head">
          <h3>{discussion.source_title || 'Untitled'}</h3>
        </div>
        <div className="card-body">
          <div className="meta">
            <a href={discussion.source_url} target="_blank" rel="noreferrer">source ↗</a>
          </div>

          <div className="chat-thread">
            {discussion.messages.map((m, i) => (
              <div key={i} className={`chat-msg chat-${m.role}`}>{m.content}</div>
            ))}
          </div>

          {committed ? (
            <p className="posted-note">
              This became a post — find it on Review to redraft or publish it.
            </p>
          ) : (
            <>
              <textarea
                className="note"
                rows={3}
                value={text}
                placeholder="Argue for it, against it, or ask about it…"
                onChange={(e) => setText(e.target.value)}
              />
              <div className="actions">
                <button className="action primary" disabled={!!busy || !text.trim()} onClick={send}>
                  {busy === 'sending' ? 'Sending…' : 'Send'}
                </button>
                <button
                  className="action"
                  disabled={!!busy || !hasArgument}
                  title={hasArgument ? '' : 'Say something first'}
                  onClick={turnIntoPost}
                >
                  {busy === 'posting' ? 'Turning into a post…' : 'Turn into a post'}
                </button>
                <button className="action danger" disabled={!!busy} onClick={abandon}>
                  {busy === 'deleting' ? 'Abandoning…' : 'Abandon'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </>
  )
}
