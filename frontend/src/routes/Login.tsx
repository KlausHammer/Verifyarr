import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import styles from './AuthPage.module.css'

export default function Login() {
  const { refresh } = useAuth()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await api.post('/auth/login', { username, password })
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed')
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
        <div className={styles.subtitle}>Log in to continue</div>
        {error && <div className="error-banner">{error}</div>}
        <div className="field">
          <label htmlFor="username">Username</label>
          <input id="username" type="text" value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
        </div>
        <button type="submit" className={`btn btn-primary ${styles.submit}`} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Log in'}
        </button>
      </form>
    </div>
  )
}
