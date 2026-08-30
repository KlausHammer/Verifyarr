import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import styles from './AuthPage.module.css'

export default function Setup() {
  const { refresh } = useAuth()
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (password.length < 5) {
      setError('Password must be at least 5 characters')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match')
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/setup', { username: 'admin', password })
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Setup failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <form className={`card ${styles.card}`} onSubmit={onSubmit}>
        <div className={styles.brand}>
          <span className={styles.brandDot} />
          verifyarr
        </div>
        <div className={styles.subtitle}>
          First visit — create a password. There is no default password, and nothing is stored in
          docker-compose.yml.
        </div>
        {error && <div className="error-banner">{error}</div>}
        <div className="field">
          <label htmlFor="password">New password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
        </div>
        <div className="field">
          <label htmlFor="confirm">Repeat password</label>
          <input id="confirm" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        <button type="submit" className={`btn btn-primary ${styles.submit}`} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Create and log in'}
        </button>
        <div className="field-hint" style={{ marginTop: 12 }}>
          Forget it later? Run <code>python3 verifyarr.py reset-password</code> in the
          container to reset it.
        </div>
      </form>
    </div>
  )
}
