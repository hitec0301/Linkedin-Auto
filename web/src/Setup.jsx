import { useState } from 'react'
import { api } from './api.js'
import { Confirm, Notice, useAsync } from './bits.jsx'

/**
 * The setup a customer has to do once, and the switches they keep.
 *
 * The LinkedIn app step is real work and there is no way to remove it: each
 * account posts through its own LinkedIn app, so that one customer's rate
 * limit or suspension is theirs alone rather than everybody's. What can be
 * removed is guesswork, so every value that has to be copied is shown here
 * ready to paste, in the order LinkedIn's own screens ask for them.
 */
export default function Setup({ me, onChange }) {
  const usage = useAsync(() => api.usage(), [])
  const [creds, setCreds] = useState({ client_id: '', client_secret: '' })
  const [redirect, setRedirect] = useState('')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const params = new URLSearchParams(window.location.search)
  const justConnected = params.get('connected') === '1'
  const connectError = params.get('connect_error')

  async function saveApp(e) {
    e.preventDefault()
    setError('')
    try {
      const result = await api.saveLinkedInApp(creds)
      setRedirect(result.redirect_uri)
      setSaved(true)
      // The secret is out of the browser's hands the moment it is stored.
      setCreds({ client_id: creds.client_id, client_secret: '' })
      onChange()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <>
      {justConnected && <Notice kind="ok">LinkedIn connected. You are ready to go.</Notice>}
      {connectError && (
        <Notice kind="error">
          LinkedIn did not complete the connection ({connectError}). Nothing was
          changed — you can try again below.
        </Notice>
      )}

      <div className="panel">
        <h3>Publishing</h3>
        <label className="switch">
          <input
            type="checkbox"
            checked={!me.paused}
            onChange={(e) => api.setPaused(!e.target.checked).then(onChange)}
          />
          Allow approved posts to go out
        </label>
        <p className="muted">
          Turning this off stops publishing within half an hour and leaves
          everything else running. It is the switch to use when you are on
          holiday, or when the news makes a scheduled post read badly.
        </p>
      </div>

      <div className="panel">
        <h3>Connect LinkedIn</h3>
        {me.linkedin_connected ? (
          <>
            <Notice kind="ok">
              Connected. This product can publish posts you have approved, and
              nothing else — it cannot read your feed, comment, or message.
            </Notice>
            <div className="actions">
              <Confirm
                className="action danger"
                label="Disconnect"
                question="Publishing stops immediately. Your drafts stay. Sure?"
                onConfirm={() => api.disconnectLinkedIn().then(onChange)}
              />
            </div>
          </>
        ) : (
          <ol className="steps">
            <li>
              <p>
                Open <a href="https://developer.linkedin.com/" target="_blank" rel="noreferrer">
                developer.linkedin.com</a> and create an app. It has to be
                associated with a LinkedIn company page — if you do not have
                one, creating a page takes about a minute and it does not have
                to be used for anything else.
              </p>
            </li>
            <li>
              <p>
                On the app's <b>Products</b> tab, request{' '}
                <b>Share on LinkedIn</b> and <b>Sign In with LinkedIn using
                OpenID Connect</b>. Both are granted automatically. Without the
                first, posting will fail with a permissions error.
              </p>
            </li>
            <li>
              <p>
                On the <b>Auth</b> tab, copy the Client ID and Client Secret in
                here.
              </p>
              {error && <Notice kind="error">{error}</Notice>}
              <form onSubmit={saveApp}>
                <label>Client ID</label>
                <input
                  type="text" required value={creds.client_id}
                  onChange={(e) => setCreds({ ...creds, client_id: e.target.value.trim() })}
                />
                <label>Client Secret</label>
                <input
                  type="password" required value={creds.client_secret}
                  placeholder={me.linkedin_app_configured ? 'stored — enter again only to replace it' : ''}
                  onChange={(e) => setCreds({ ...creds, client_secret: e.target.value.trim() })}
                />
                <div className="actions">
                  <button className="action primary" type="submit">Save</button>
                </div>
              </form>
            </li>
            <li>
              <p>
                Back on the <b>Auth</b> tab, add this exact address under{' '}
                <b>Authorized redirect URLs</b>:
              </p>
              <p>
                <code>{redirect || `${window.location.origin}/auth/linkedin/connect/callback`}</code>
              </p>
              <p className="muted">
                It must match character for character, including https and the
                absence of a trailing slash. A mismatch is the single most
                common reason the next step fails.
              </p>
            </li>
            <li>
              <p>Then authorise this product to post as you.</p>
              <div className="actions">
                <a
                  className="action primary"
                  href="/auth/linkedin/connect"
                  style={{ textDecoration: 'none', display: 'inline-block' }}
                  aria-disabled={!me.linkedin_app_configured}
                  onClick={(e) => {
                    if (!me.linkedin_app_configured && !saved) {
                      e.preventDefault()
                      setError('Save your Client ID and Secret first.')
                    }
                  }}
                >
                  Authorise posting
                </a>
              </div>
            </li>
          </ol>
        )}
      </div>

      <div className="panel">
        <h3>This month</h3>
        {usage.data ? (
          <>
            <div className="stat-grid">
              <div className="stat">
                <b>{usage.data.total_tokens.toLocaleString()}</b>
                <span>of {usage.data.cap.toLocaleString()} drafting tokens</span>
              </div>
              <div className="stat">
                <b>{me.post_count}</b><span>posts published, all time</span>
              </div>
              <div className="stat">
                <b>{me.plan}</b><span>plan ({me.status})</span>
              </div>
            </div>
            <div className="bar">
              <i
                className={usage.data.fraction_used >= 1 ? 'over' : ''}
                style={{ width: `${Math.min(100, usage.data.fraction_used * 100)}%` }}
              />
            </div>
            {usage.data.fraction_used >= 0.8 && (
              <p className="muted">
                Drafting stops if this runs out. Approving and publishing what
                you already have are not affected.
              </p>
            )}
          </>
        ) : (
          <p className="muted">No drafting yet this month.</p>
        )}
      </div>

      <div className="panel">
        <h3>What this product will not do</h3>
        <ul className="muted">
          <li>It never publishes anything you have not approved.</li>
          <li>It does not comment, react, message, or follow anyone.</li>
          <li>It does not read your feed or scrape LinkedIn.</li>
          <li>
            There is no unattended mode, and adding one is not on the roadmap.
            The approval step is the product.
          </li>
        </ul>
      </div>
    </>
  )
}
