import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { AttentionResponse, NextRunResponse, StatsSummary } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { useRunningJob } from '../hooks/useRunningJob'
import { formatExact, nextScanLabel } from '../lib/format'
import { GENERATE_UI } from '../lib/features'
import { runTargetLabel, runTypeLabel } from '../lib/runLabels'

// Settings submenu, shown expanded under the Settings nav item (Bazarr-style) instead of as
// horizontal tabs on the page itself. Labels are display-only; keys are the real API groups.
export const SETTINGS_TABS = [
  { key: 'general', label: 'General' },
  { key: 'sync', label: 'Sync' },
  { key: 'correctness', label: 'Speech recognition' },
  { key: 'generate', label: 'Generate missing' },
  { key: 'automation', label: 'Automation' },
  { key: 'bazarr', label: 'Bazarr' },
  { key: 'scheduling', label: 'Scheduling' },
  { key: 'log', label: 'Log' },
  { key: 'account', label: 'Account' },
].filter((t) => GENERATE_UI || t.key !== 'generate')

const NAV_ITEMS: { to: string; label: string; end?: boolean }[] = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/movies', label: 'Movies' },
  { to: '/series', label: 'Series' },
  { to: '/files', label: 'Files' },
  { to: '/activity', label: 'Activity' },
  { to: '/stats', label: 'Stats' },
  { to: '/quarantine', label: 'Quarantine & Backups' },
  { to: '/bazarr-blacklist', label: 'Bazarr blacklist' },
]

const topLink = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
  width: '100%',
  border: 0,
  padding: '8px 14px 8px 13px',
  textAlign: 'left',
  fontSize: 14,
  borderLeft: `3px solid ${active ? 'var(--accent)' : 'transparent'}`,
  background: active ? 'var(--bg-hover)' : 'transparent',
  color: active ? 'var(--text)' : 'var(--text-dim)',
  fontWeight: active ? 600 : 500,
  textDecoration: 'none',
})

export default function Layout() {
  const { status, logout } = useAuth()
  const { run, isRunning } = useRunningJob()
  const location = useLocation()
  const navigate = useNavigate()
  const [menuOpen, setMenuOpen] = useState(false)
  const [attention, setAttention] = useState(0)
  const [nextScan, setNextScan] = useState<string | null | undefined>(undefined)
  const onSettings = location.pathname.startsWith('/settings')

  // Files badge: flagged files plus videos with no subtitle at all.
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [att, summary] = await Promise.all([
          api.get<AttentionResponse>('/stats/attention'),
          api.get<StatsSummary>('/stats/summary'),
        ])
        if (!cancelled) {
          setAttention(att.items.reduce((s, i) => s + i.count, 0) + (summary.files.missing ?? 0))
        }
      } catch {
        if (!cancelled) setAttention(0)
      }
    }
    load()
    const id = setInterval(load, 30000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const r = await api.get<NextRunResponse>('/runs/next')
        if (!cancelled) setNextScan(r.next_run_at)
      } catch {
        if (!cancelled) setNextScan(null)
      }
    }
    load()
    const id = setInterval(load, 5 * 60000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  // File detail and job detail highlight their parent section.
  const section = location.pathname.startsWith('/files/') ? '/files'
    : location.pathname.startsWith('/activity/') ? '/activity'
    : location.pathname

  const pct = run && run.files_total ? Math.round((run.files_processed / run.files_total) * 100) : null
  const runAria = run ? `${runTypeLabel(run)} running${pct !== null ? `, ${pct}% done` : ''}. Open job` : ''

  // Toasts live one level up (App), so the setup wizard can toast across its own
  // navigation too — the viewport renders from there.
  return (
      <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--bg)', color: 'var(--text)', fontSize: 14 }}>
        <div data-scrim data-open={menuOpen ? 'true' : 'false'} onClick={() => setMenuOpen(false)} style={{ display: 'none', position: 'fixed', inset: 0, background: 'rgba(0,0,0,.55)', zIndex: 45 }} />
        <nav data-sidebar data-open={menuOpen ? 'true' : 'false'} aria-label="Main" style={{ width: 214, flex: 'none', background: 'var(--bg-sidebar)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', position: 'sticky', top: 0, height: '100vh', overflowY: 'auto' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '15px 16px', borderBottom: '1px solid var(--border)' }}>
            <span aria-hidden="true" style={{ width: 13, height: 13, background: 'var(--accent)', transform: 'rotate(45deg)', borderRadius: 2, flex: 'none' }} />
            <span style={{ fontWeight: 700, fontSize: 16, letterSpacing: 0.2 }}>Verifyarr</span>
            <button onClick={() => setMenuOpen(false)} data-mobilebar aria-label="Close menu" style={{ display: 'none', marginLeft: 'auto', background: 'none', border: 0, color: 'var(--text-dim)', fontSize: 18 }}>
              ×
            </button>
          </div>
          <div style={{ padding: '8px 0', flex: 1, display: 'flex', flexDirection: 'column' }}>
            {NAV_ITEMS.map((item) => {
              const active = item.end ? section === item.to : section === item.to || section.startsWith(`${item.to}/`)
              const badge = item.to === '/files' && attention > 0 ? String(attention) : ''
              return (
                <NavLink key={item.to} to={item.to} end={item.end} data-hover aria-current={active ? 'page' : undefined} onClick={() => setMenuOpen(false)} style={() => topLink(active)}>
                  <span>{item.label}</span>
                  {badge && <span className="pill pill-bad" style={{ padding: '0 7px', fontSize: 11 }} title={`${attention} subtitles need attention`}>{badge}</span>}
                </NavLink>
              )
            })}
            <NavLink to="/settings/general" data-hover onClick={() => setMenuOpen(false)} style={() => topLink(onSettings)}>
              <span>Settings</span>
            </NavLink>
            {onSettings && SETTINGS_TABS.map((t) => {
              const to = `/settings/${t.key}`
              const active = location.pathname === to
              return (
                <NavLink
                  key={t.key}
                  to={to}
                  data-hover={active ? undefined : ''}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => setMenuOpen(false)}
                  style={{
                    display: 'block', width: '100%', border: 0, borderLeft: '3px solid transparent',
                    background: 'transparent', padding: '5px 14px 5px 30px', textAlign: 'left',
                    fontSize: 13, color: active ? 'var(--accent)' : 'var(--text-dim)',
                    fontWeight: active ? 600 : 400, textDecoration: 'none',
                  }}
                >
                  {t.label}
                </NavLink>
              )
            })}
          </div>
          <div style={{ padding: 10, borderTop: '1px solid var(--border)' }}>
            {isRunning && run ? (
              <button
                onClick={() => { setMenuOpen(false); navigate(`/activity/${run.id}`) }}
                aria-label={runAria}
                style={{ width: '100%', textAlign: 'left', background: 'var(--bg-elevated)', border: '1px solid var(--accent-dim)', borderRadius: 'var(--radius)', padding: '9px 10px', display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text)', cursor: 'pointer' }}
              >
                <span style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, fontWeight: 600 }}>
                  <span className="spinner" style={{ width: 11, height: 11, flex: 'none' }} />
                  <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{runTypeLabel(run)}</span>
                  {pct !== null && <span style={{ color: 'var(--accent)' }}>{pct}%</span>}
                </span>
                <span style={{ fontSize: 12, color: 'var(--text-dim)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{runTargetLabel(run)}</span>
                {pct !== null && (
                  <span style={{ height: 4, background: 'var(--border)', borderRadius: 2, overflow: 'hidden', display: 'block' }}>
                    <svg aria-hidden="true" width="100%" height="4" style={{ display: 'block' }}>
                      <rect x="0" y="0" width={`${pct}%`} height="4" style={{ fill: 'var(--accent)' }} />
                    </svg>
                  </span>
                )}
                <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                  {run.files_total ? `${run.files_processed} of ${run.files_total} files` : `${run.files_processed} files`}
                </span>
              </button>
            ) : (
              <div style={{ fontSize: 12, color: 'var(--text-dim)', padding: '2px 6px', lineHeight: 1.6 }}>
                <div>No job running</div>
                {nextScan !== undefined && <div title={nextScan ? formatExact(nextScan) : ''}>Next scan: {nextScanLabel(nextScan)}</div>}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 6px 0', fontSize: 12.5, color: 'var(--text-dim)' }}>
              <span>{status?.username ?? '—'}</span>
              <button onClick={() => logout()} style={{ background: 'none', border: 0, color: 'var(--text-dim)', fontSize: 12.5, padding: '2px 0', cursor: 'pointer' }}>
                Log out
              </button>
            </div>
          </div>
        </nav>
        <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          <div data-mobilebar style={{ display: 'none', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--border)', background: 'var(--bg-sidebar)', position: 'sticky', top: 0, zIndex: 30 }}>
            <button className="btn btn-sm" onClick={() => setMenuOpen(true)} aria-label="Open menu" aria-expanded={menuOpen ? 'true' : 'false'}>
              ☰ Menu
            </button>
            <span style={{ fontWeight: 700 }}>Verifyarr</span>
            {isRunning && run && (
              <button className="btn btn-sm" style={{ marginLeft: 'auto' }} onClick={() => navigate(`/activity/${run.id}`)} aria-label={runAria}>
                <span className="spinner" style={{ width: 11, height: 11 }} />
                {pct !== null ? `${pct}%` : '…'}
              </button>
            )}
          </div>
          <Outlet />
        </main>
      </div>
  )
}
