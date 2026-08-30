import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useRunningJob } from '../hooks/useRunningJob'
import styles from './Layout.module.css'

const NAV_ITEMS: { to: string; label: string; icon: string; end?: boolean }[] = [
  { to: '/movies', label: 'Movies', icon: '▦' },
  { to: '/series', label: 'Series', icon: '▥' },
  { to: '/files', label: 'Files', icon: '▤' },
  { to: '/activity', label: 'Activity', icon: '▶' },
  { to: '/stats', label: 'Stats', icon: '▲' },
  { to: '/quarantine', label: 'Quarantine', icon: '▣' },
  { to: '/bazarr-blacklist', label: 'Bazarr blacklist', icon: '⛔' },
]

// Settings submenu, shown expanded under the Settings nav item (Bazarr-style) instead of as
// horizontal tabs on the page itself.
export const SETTINGS_TABS = [
  { key: 'general', label: 'General' },
  { key: 'sync', label: 'Sync' },
  { key: 'correctness', label: 'LLM settings' },
  { key: 'automation', label: 'Automation' },
  { key: 'bazarr', label: 'Bazarr' },
  { key: 'scheduling', label: 'Scheduling' },
  { key: 'log', label: 'Log' },
  { key: 'account', label: 'Account' },
]

export default function Layout() {
  const { status, logout } = useAuth()
  const { isRunning, currentRunId } = useRunningJob()
  const location = useLocation()
  const onSettings = location.pathname.startsWith('/settings')

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <span className={styles.brandDot} />
          verifyarr
        </div>
        <nav className={styles.nav}>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navLinkActive : ''}`}
            >
              <span className={styles.navIcon}>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
          <NavLink
            to="/settings/general"
            className={`${styles.navLink} ${onSettings ? styles.navLinkActive : ''}`}
          >
            <span className={styles.navIcon}>⚙</span>
            Settings
          </NavLink>
          {onSettings && (
            <div className={styles.subNav}>
              {SETTINGS_TABS.map((t) => (
                <NavLink
                  key={t.key}
                  to={`/settings/${t.key}`}
                  className={({ isActive }) => `${styles.subNavLink} ${isActive ? styles.navLinkActive : ''}`}
                >
                  {t.label}
                </NavLink>
              ))}
            </div>
          )}
        </nav>
        <div className={styles.footer}>
          <div className={styles.footerRow}>
            <span>{status?.username ?? '—'}</span>
            <button className={styles.logoutBtn} onClick={() => logout()}>
              Log out
            </button>
          </div>
        </div>
      </aside>
      <div className={styles.main}>
        <div className={styles.topbar}>
          <div />
          {isRunning && (
            <NavLink to={`/activity/${currentRunId}`} className={styles.runningBadge}>
              <span className="spinner" />
              Job running — view log
            </NavLink>
          )}
        </div>
        <div className={styles.content}>
          <Outlet />
        </div>
      </div>
    </div>
  )
}
