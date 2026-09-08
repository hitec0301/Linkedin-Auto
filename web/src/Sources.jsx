import { useState } from 'react'
import { api } from './api.js'
import { Confirm, Notice, useAsync } from './bits.jsx'

const TIERS = {
  1: 'Tier 1 — primary research and practitioner reporting',
  2: 'Tier 2 — trade press',
  3: 'Tier 3 — aggregators and commentary',
}

const BLANK = { name: '', url: '', tier: 2, audience: '', active: true }

export default function Sources({ me, go }) {
  const sources = useAsync(() => api.sources(), [])
  const [draft, setDraft] = useState(BLANK)
  const [error, setError] = useState('')

  async function add(e) {
    e.preventDefault()
    setError('')
    try {
      await api.addSource(draft)
      setDraft(BLANK)
      sources.reload()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <>
      <div className="panel">
        <h3>Where candidates come from</h3>
        <p className="muted">
          RSS or Atom feeds, read once a week. Tier is how much a source counts
          when candidates are ranked, not how often it is read — a tier 1 feed
          does not get you more posts, it gets its items taken more seriously.
        </p>

        {me?.audience_description && (
          <p className="muted">
            Your audience, from Setup: "{me.audience_description}" — add feeds
            that speak to that.{' '}
            <a href="#" onClick={(e) => { e.preventDefault(); go('setup') }}>Edit it</a>.
            Finding the feeds themselves is still on you; this product does not
            crawl the web to guess at them.
          </p>
        )}

        {sources.error && <Notice kind="error">{sources.error}</Notice>}
        {error && <Notice kind="error">{error}</Notice>}

        <table>
          <thead>
            <tr><th>Source</th><th>Tier</th><th>Last read</th><th /></tr>
          </thead>
          <tbody>
            {(sources.data || []).map((s) => (
              <tr key={s.id}>
                <td>
                  <div>{s.name}</div>
                  <div className="muted">{s.url}</div>
                  {s.last_error && <div className="muted" style={{ color: 'var(--danger)' }}>{s.last_error}</div>}
                </td>
                <td>{s.tier}</td>
                <td className="muted">
                  {s.last_ok_at ? s.last_ok_at.slice(0, 10) : 'not yet'}
                </td>
                <td className="right">
                  <Confirm
                    label="Remove"
                    question="Remove this feed?"
                    onConfirm={() => api.deleteSource(s.id).then(sources.reload)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {(sources.data || []).length === 0 && !sources.loading && (
          <p className="empty">
            No feeds yet. Curation has nothing to read until you add at least one.
          </p>
        )}
      </div>

      <form className="panel" onSubmit={add}>
        <h3>Add a feed</h3>
        <label>Name</label>
        <input
          type="text" value={draft.name} required
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        />
        <label>Feed address</label>
        <input
          type="url" value={draft.url} required placeholder="https://example.com/feed"
          onChange={(e) => setDraft({ ...draft, url: e.target.value })}
        />
        <label>Tier</label>
        <select value={draft.tier} onChange={(e) => setDraft({ ...draft, tier: Number(e.target.value) })}>
          {Object.entries(TIERS).map(([n, label]) => (
            <option key={n} value={n}>{label}</option>
          ))}
        </select>
        <div className="actions">
          <button className="action primary" type="submit">Add</button>
        </div>
      </form>
    </>
  )
}
