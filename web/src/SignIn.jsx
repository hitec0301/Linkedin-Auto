export default function SignIn() {
  const failed = new URLSearchParams(window.location.search).get('signin_error')
  return (
    <div className="shell">
      <div className="signin">
        <h1>Publishing pipeline</h1>
        <p>
          Three or four posts a week, in your voice, that you approve one at a
          time. Nothing is ever published without you pressing approve.
        </p>
        {failed && (
          <p className="notice error">
            Sign-in did not complete ({failed}). Nothing was changed — try again.
          </p>
        )}
        <p>
          <a className="action primary" href="/auth/linkedin/start"
             style={{ textDecoration: 'none', display: 'inline-block' }}>
            Sign in with LinkedIn
          </a>
        </p>
        <p className="muted">
          Signing in only identifies you. Permission to post is a separate step,
          later, and you can withdraw it at any time.
        </p>
      </div>
    </div>
  )
}
