import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAuth } from '../hooks/useAuth'

export default function Login() {
  const { refresh } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (!username.trim() || !password) {
      setError('Enter your username and password.')
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/login', { username: username.trim(), password })
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed')
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
      <form className="card" onSubmit={onSubmit} style={{ width: 380, maxWidth: '100%', padding: 24 }}>
        <h1 style={{ fontSize: 19, margin: '0 0 18px' }}>Log in</h1>
        <div className="field">
          <label htmlFor="li-u">Username</label>
          <input id="li-u" type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="li-p">Password</label>
          <input id="li-p" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <div className="error-banner" role="alert" style={{ marginBottom: 14 }}>{error}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy} style={{ width: '100%', justifyContent: 'center', padding: '9px 14px' }}>
          {busy && <span className="spinner" style={{ borderTopColor: '#082e28', width: 12, height: 12 }} />}
          {busy ? 'Logging in…' : 'Log in'}
        </button>
        <p className="text-dim" style={{ fontSize: 12.5, margin: '14px 0 0', lineHeight: 1.5 }}>
          Forgot your password? Run <span className="mono">python3 verifyarr.py reset-password</span> inside the container to reset it.
        </p>
      </form>
    </div>
  )
}
