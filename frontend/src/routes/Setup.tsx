import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useAuth } from '../hooks/useAuth'

// Must match verifyarr.auth.MIN_PASSWORD_LENGTH (the server enforces it too).
const MIN_PASSWORD_LENGTH = 5

export default function Setup() {
  const { refresh } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [p1, setP1] = useState('')
  const [p2, setP2] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (!username.trim()) {
      setError('Choose a username.')
      return
    }
    if (p1.length < MIN_PASSWORD_LENGTH) {
      setError(`The password needs at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (p1 !== p2) {
      setError("The two passwords don't match.")
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/setup', { username: username.trim(), password: p1 })
      await refresh()
      navigate('/wizard', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Setup failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)', color: 'var(--text)', fontSize: 14, display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '48px 16px 32px', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 22 }}>
        <span aria-hidden="true" style={{ width: 15, height: 15, background: 'var(--accent)', transform: 'rotate(45deg)', borderRadius: 2 }} />
        <span style={{ fontWeight: 700, fontSize: 20 }}>Verifyarr</span>
      </div>
      <form className="card" onSubmit={onSubmit} style={{ width: 400, maxWidth: '100%', padding: 24 }}>
        <h1 style={{ fontSize: 19, margin: '0 0 4px' }}>Create your account</h1>
        <p className="text-dim" style={{ margin: '0 0 18px', fontSize: 13, lineHeight: 1.5 }}>
          This account protects the Verifyarr web page. You&apos;ll use it to log in from now on.
        </p>
        <div className="field">
          <label htmlFor="su-u">Username</label>
          <input id="su-u" type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="su-p">Password</label>
          <input id="su-p" type="password" autoComplete="new-password" value={p1} onChange={(e) => setP1(e.target.value)} />
          <div className="field-hint" style={{ color: 'var(--text-dim)' }}>At least {MIN_PASSWORD_LENGTH} characters.</div>
        </div>
        <div className="field">
          <label htmlFor="su-r">Repeat password</label>
          <input id="su-r" type="password" autoComplete="new-password" value={p2} onChange={(e) => setP2(e.target.value)} />
        </div>
        {error && <div className="error-banner" role="alert" style={{ marginBottom: 14 }}>{error}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy} style={{ width: '100%', justifyContent: 'center', padding: '9px 14px' }}>
          {busy ? <span className="spinner" /> : 'Create account and continue'}
        </button>
      </form>
    </div>
  )
}
