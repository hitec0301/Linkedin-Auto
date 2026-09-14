import { useEffect, useRef, useState } from 'react'

export function Notice({ kind = 'warn', children }) {
  return <div className={`notice ${kind === 'warn' ? '' : kind}`}>{children}</div>
}

export function useAsync(loader, deps = []) {
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    setState((s) => ({ ...s, loading: true }))
    loader()
      .then((data) => live && setState({ data, error: '', loading: false }))
      .catch((err) => live && setState({ data: null, error: err.message, loading: false }))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  return { ...state, reload: () => setNonce((n) => n + 1) }
}

/**
 * A text field that saves a short time after typing stops.
 *
 * Autosave rather than a Save button, because every field it is used on is a
 * field somebody edits in passing — an angle, a tick — and losing that to a
 * forgotten button is exactly the kind of small betrayal that makes a tool
 * feel unreliable. The status line is there so saving is visible, not silent.
 */
export function AutoSave({ value, onSave, placeholder, rows = 2, id }) {
  const [text, setText] = useState(value ?? '')
  const [state, setState] = useState('idle')
  const timer = useRef(null)
  const lastSaved = useRef(value ?? '')

  useEffect(() => {
    setText(value ?? '')
    lastSaved.current = value ?? ''
  }, [value, id])

  function change(next) {
    setText(next)
    setState('editing')
    clearTimeout(timer.current)
    timer.current = setTimeout(async () => {
      if (next === lastSaved.current) { setState('idle'); return }
      setState('saving')
      try {
        await onSave(next)
        lastSaved.current = next
        setState('saved')
      } catch (err) {
        setState(`error: ${err.message}`)
      }
    }, 700)
  }

  return (
    <div>
      <textarea
        rows={rows}
        value={text}
        placeholder={placeholder}
        onChange={(e) => change(e.target.value)}
      />
      <div className="muted right">
        {state === 'saving' && 'saving…'}
        {state === 'saved' && 'saved'}
        {state.startsWith?.('error') && state}
      </div>
    </div>
  )
}

export function Confirm({ label, question, onConfirm, className = 'action' }) {
  const [asking, setAsking] = useState(false)
  if (!asking) {
    return <button className={className} onClick={() => setAsking(true)}>{label}</button>
  }
  return (
    <span className="switch">
      <span className="muted">{question}</span>
      <button className="action danger" onClick={() => { setAsking(false); onConfirm() }}>
        Yes
      </button>
      <button className="action" onClick={() => setAsking(false)}>No</button>
    </span>
  )
}
