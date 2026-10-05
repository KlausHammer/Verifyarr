import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type {
  AppLogLine,
  AutomationSettings,
  BazarrSettings,
  CorrectnessSettings,
  GeneralSettings,
  GenerateSettings,
  LibraryResponse,
  LogSettings,
  NextRunResponse,
  SchedulingSettings,
  SyncSettings,
} from '../api/types'
import { buildCron, parseCron, DAY_NAMES, type FriendlySchedule, type ScheduleMode } from '../lib/cron'
import { formatRelative } from '../lib/format'
import ConfirmDialog from '../components/ConfirmDialog'
import FolderBrowser from '../components/FolderBrowser'
import CopyLogButton from '../components/CopyLogButton'
import { SETTINGS_TABS } from '../components/Layout'
import { GENERATE_UI } from '../lib/features'
import { useAuth } from '../hooks/useAuth'
import { useAutoScrollLog } from '../hooks/useAutoScrollLog'
import { useToasts } from '../hooks/useToasts'

// The toolbar's Save/Discard buttons are driven by the ACTIVE tab through this: each tab
// reports its dirty/error/saved state (primitives only, so the effect below can't loop)
// and stashes its save/discard in a ref the toolbar calls.
interface ToolState {
  dirty: boolean
  error: string | null
  savedAt: number | null
  hasAdvanced: boolean
  saving: boolean
}
interface ToolActions {
  save: () => void
  discard: () => void
}
const ToolbarCtx = createContext<{ setTool: (t: ToolState) => void; actions: { current: ToolActions } } | null>(null)

function useToolbarApi(dirty: boolean, error: string | null, savedAt: number | null, hasAdvanced: boolean, saving: boolean, save: () => void, discard: () => void) {
  const ctx = useContext(ToolbarCtx)
  if (!ctx) throw new Error('settings tab outside ToolbarCtx')
  ctx.actions.current = { save, discard }
  // Depend on setTool, not ctx: the provider's value object is new every render (loop).
  const { setTool } = ctx
  useEffect(() => {
    setTool({ dirty, error, savedAt, hasAdvanced, saving })
  }, [setTool, dirty, error, savedAt, hasAdvanced, saving])
}

function useGroup<T>(group: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)

  function load() {
    setData(null)
    api
      .get<T>(`/settings/${group}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [group])
  return { data, setData, error, setError, reload: load }
}

// Backend still stores/accepts one url string (scheme optional, added server-side) — these just
// split it into two fields for editing and join it back on save/test.
function urlToHostPort(url: string): { host: string; port: string } {
  if (!url) return { host: '', port: '' }
  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url) ? url : `http://${url}`
  try {
    const u = new URL(withScheme)
    return { host: u.hostname, port: u.port }
  } catch {
    return { host: url, port: '' }
  }
}
function hostPortToUrl(host: string, port: string): string {
  if (!host) return ''
  return port ? `${host}:${port}` : host
}

const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
const MINUTES = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'))

// Native <input type="time"> renders an unstyleable browser widget that clashes with the dark
// theme and is fiddly to use — plain <select>s match the rest of the UI and work everywhere.
function TimeOfDayField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [hh, mm] = value.split(':')
  const hour = HOURS.includes(hh) ? hh : '04'
  const minute = MINUTES.includes(mm) ? mm : '00'
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', maxWidth: 200 }}>
      <select value={hour} onChange={(e) => onChange(`${e.target.value}:${minute}`)}>
        {HOURS.map((h) => (
          <option key={h} value={h}>{h}</option>
        ))}
      </select>
      <span style={{ color: 'var(--text-dim)' }}>:</span>
      <select value={minute} onChange={(e) => onChange(`${hour}:${e.target.value}`)}>
        {MINUTES.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>
    </div>
  )
}

function HostPortFields({ host, port, onHost, onPort }: { host: string; port: string; onHost: (v: string) => void; onPort: (v: string) => void }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 140px', gap: 12 }}>
      <Field label="Host / IP">
        <input type="text" value={host} onChange={(e) => onHost(e.target.value)} placeholder="192.168.1.32" />
      </Field>
      <Field label="Port">
        <input type="text" value={port} onChange={(e) => onPort(e.target.value)} placeholder="8989" />
      </Field>
    </div>
  )
}

// Simple shows what most people change; Advanced adds the knobs that still change results or
// cost. Internal calibration stays out of both.
type SettingsMode = 'simple' | 'advanced'
const MODE_KEY = 'verifyarr.settingsMode'
const ModeContext = createContext<SettingsMode>('simple')

function readMode(): SettingsMode {
  try {
    return localStorage.getItem(MODE_KEY) === 'advanced' ? 'advanced' : 'simple'
  } catch {
    return 'simple'
  }
}

function Advanced({ children }: { children: ReactNode }) {
  return useContext(ModeContext) === 'advanced' ? <>{children}</> : null
}

function Field({ label, tip, advanced, children }: { label: string; tip?: string; advanced?: boolean; children: ReactNode }) {
  const mode = useContext(ModeContext)
  const [tipOpen, setTipOpen] = useState(false)
  if (advanced && mode !== 'advanced') return null
  return (
    <div className="field" style={{ maxWidth: 540 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 5 }}>
        <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 500 }}>{label}</span>
        {tip && (
          <button
            type="button"
            aria-label={`What is "${label}"?`}
            aria-expanded={tipOpen}
            title={tip}
            onClick={() => setTipOpen((o) => !o)}
            style={{ width: 18, height: 18, borderRadius: '50%', border: '1px solid var(--border)', background: 'var(--bg-hover)', color: 'var(--text-dim)', fontSize: 11, fontWeight: 700, padding: 0, lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: 'none', cursor: 'pointer' }}
          >
            ?
          </button>
        )}
        {advanced && (
          <span style={{ fontSize: 11, color: 'var(--text-dim)', border: '1px solid var(--border)', borderRadius: 3, padding: '0 5px' }}>Advanced</span>
        )}
      </div>
      {tip && tipOpen && <div className="field-hint" style={{ color: 'var(--text-dim)', margin: '0 0 7px' }}>{tip}</div>}
      {children}
    </div>
  )
}

function ToggleRow({ id, checked, onChange, label, tip, advanced, disabled }: { id: string; checked: boolean; onChange: (v: boolean) => void; label: string; tip?: string; advanced?: boolean; disabled?: boolean }) {
  const mode = useContext(ModeContext)
  const [tipOpen, setTipOpen] = useState(false)
  if (advanced && mode !== 'advanced') return null
  return (
    <div className="field" style={{ maxWidth: 540 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: 0, color: 'var(--text)', fontSize: 14, fontWeight: 400, cursor: disabled ? undefined : 'pointer' }}>
          <input id={id} type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)', margin: 0 }} />
          {label}
        </label>
        {tip && (
          <button
            type="button"
            aria-label={`What is "${label}"?`}
            aria-expanded={tipOpen}
            title={tip}
            onClick={() => setTipOpen((o) => !o)}
            style={{ width: 18, height: 18, borderRadius: '50%', border: '1px solid var(--border)', background: 'var(--bg-hover)', color: 'var(--text-dim)', fontSize: 11, fontWeight: 700, padding: 0, lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: 'none', cursor: 'pointer' }}
          >
            ?
          </button>
        )}
        {advanced && (
          <span style={{ fontSize: 11, color: 'var(--text-dim)', border: '1px solid var(--border)', borderRadius: 3, padding: '0 5px' }}>Advanced</span>
        )}
      </div>
      {tip && tipOpen && <div className="field-hint" style={{ color: 'var(--text-dim)', margin: '7px 0 0' }}>{tip}</div>}
    </div>
  )
}

function ChoiceField<T extends string>({ label, tip, advanced, name, options, value, onPick }: {
  label: string
  tip?: string
  advanced?: boolean
  name: string
  options: { value: T; title: string; desc: string }[]
  value: T
  onPick: (v: T) => void
}) {
  return (
    <Field label={label} tip={tip} advanced={advanced}>
      <div role="radiogroup" aria-label={label} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {options.map((o) => {
          const sel = value === o.value
          return (
            <label
              key={o.value}
              style={{
                display: 'flex', gap: 9, alignItems: 'flex-start', padding: '8px 10px',
                border: `1px solid ${sel ? 'var(--accent-dim)' : 'var(--border)'}`,
                background: sel ? 'rgba(94,234,212,.06)' : 'transparent',
                borderRadius: 'var(--radius)', cursor: 'pointer', margin: 0, color: 'var(--text)',
                fontSize: 14, fontWeight: 400,
              }}
            >
              <input type="radio" name={name} checked={sel} onChange={() => onPick(o.value)} style={{ marginTop: 3, accentColor: 'var(--accent)' }} />
              <span><span style={{ fontWeight: 600 }}>{o.title}</span><span className="text-dim" style={{ fontSize: 13 }}> · {o.desc}</span></span>
            </label>
          )
        })}
      </div>
    </Field>
  )
}

interface PathHealth {
  exists: boolean
  entry_count: number | null
}

function SingleFolderField({ label, tip, path, onChange }: { label: string; tip?: string; path: string; onChange: (path: string) => void }) {
  const [browsing, setBrowsing] = useState(false)
  const [health, setHealth] = useState<PathHealth | null>(null)
  const [tipOpen, setTipOpen] = useState(false)

  useEffect(() => {
    if (!path) { setHealth(null); return }
    api
      .get<PathHealth>(`/browse/check?path=${encodeURIComponent(path)}`)
      .then(setHealth)
      .catch(() => setHealth({ exists: false, entry_count: null }))
  }, [path])

  return (
    <div className="field" style={{ maxWidth: 540 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 5 }}>
        <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 500 }}>{label}</span>
        {tip && (
          <button
            type="button"
            aria-label={`What is "${label}"?`}
            aria-expanded={tipOpen}
            title={tip}
            onClick={() => setTipOpen((o) => !o)}
            style={{ width: 18, height: 18, borderRadius: '50%', border: '1px solid var(--border)', background: 'var(--bg-hover)', color: 'var(--text-dim)', fontSize: 11, fontWeight: 700, padding: 0, lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: 'none', cursor: 'pointer' }}
          >
            ?
          </button>
        )}
      </div>
      {tip && tipOpen && <div className="field-hint" style={{ color: 'var(--text-dim)', margin: '0 0 7px' }}>{tip}</div>}
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8, background: 'var(--bg)',
          border: '1px solid var(--border)', borderRadius: 6, padding: '6px 10px',
        }}
      >
        <span className="mono" style={{ flex: 1, fontSize: 12.5, overflowWrap: 'anywhere' }}>
          {path || <span className="text-faint">Not set</span>}
        </span>
        {path && health === null && <span className="spinner" />}
        {path && health?.exists && <span className="pill pill-ok">found</span>}
        {path && health && !health.exists && <span className="pill pill-bad">not found</span>}
        <button type="button" className="btn btn-sm" onClick={() => setBrowsing(true)}>
          {path ? 'Change' : 'Set folder'}
        </button>
        {path && (
          <button type="button" className="btn btn-sm btn-danger" onClick={() => onChange('')}>
            Clear
          </button>
        )}
      </div>
      {browsing && (
        <FolderBrowser initialPath={path || '/media'} onSelect={(p) => { onChange(p); setBrowsing(false) }} onClose={() => setBrowsing(false)} />
      )}
    </div>
  )
}

// One switch per check (Sync / Correctness / Line-order), one column per way a scan can start —
// renders inside AutomationTab's own single save (below), spanning three settings groups
// (general/sync/correctness) on top of that tab's own "automation" group.
function useWhatRuns() {
  const general = useGroup<GeneralSettings>('general')
  const sync = useGroup<SyncSettings>('sync')
  const correctness = useGroup<CorrectnessSettings>('correctness')
  const generate = useGroup<GenerateSettings>('generate')
  const loadError = general.error || sync.error || correctness.error || generate.error
  return { general, sync, correctness, generate, loadError }
}

const AUTO_ACTION_TIP =
  'off = flag only. quarantine = move it aside. blacklist = + tell Bazarr. remediate = + fetch a replacement.'

function AutoActionSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Action if SUSPECT">
      <option value="off">off</option>
      <option value="quarantine">quarantine</option>
      <option value="blacklist">blacklist</option>
      <option value="remediate">remediate</option>
    </select>
  )
}

function WhatRunsTable({ general, sync, correctness, generate }: ReturnType<typeof useWhatRuns>) {
  const [tipOpen, setTipOpen] = useState<string | null>(null)
  if (!general.data || !sync.data || !correctness.data || !generate.data) return <span className="spinner" />
  const g = general.data, s = sync.data, c = correctness.data, gen = generate.data

  const tipBtn = (key: string, label: string, text: string) => (
    <button
      type="button"
      aria-label={`What is "${label}"?`}
      aria-expanded={tipOpen === key}
      title={text}
      onClick={() => setTipOpen((o) => (o === key ? null : key))}
      style={{ width: 18, height: 18, borderRadius: '50%', border: '1px solid var(--border)', background: 'var(--bg-hover)', color: 'var(--text-dim)', fontSize: 11, fontWeight: 700, padding: 0, lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: 'none', cursor: 'pointer', marginLeft: 7 }}
    >
      ?
    </button>
  )

  const rows = [
    {
      key: 'sync', name: 'Sync', tip: 'Fixes subtitle timing to match the audio.',
      manual: s.enabled, setManual: (v: boolean) => sync.setData({ ...s, enabled: v }),
      auto: g.auto_scan_sync_enabled, setAuto: (v: boolean) => general.setData({ ...g, auto_scan_sync_enabled: v }),
      action: <span className="text-faint">—</span>,
    },
    {
      key: 'correctness', name: 'Correctness check',
      tip: 'Compares a bit of audio to the subtitle with Whisper, to catch a mismatched file. Runs on this machine with local Whisper; no API key needed.',
      manual: c.enabled, setManual: (v: boolean) => correctness.setData({ ...c, enabled: v }),
      auto: g.auto_scan_correctness_enabled, setAuto: (v: boolean) => general.setData({ ...g, auto_scan_correctness_enabled: v }),
      action: (
        <span style={{ display: 'flex', gap: 7, alignItems: 'center' }}>
          <AutoActionSelect value={c.auto_action} onChange={(v) => correctness.setData({ ...c, auto_action: v as CorrectnessSettings['auto_action'] })} />
          {tipBtn('action', 'Action if SUSPECT', AUTO_ACTION_TIP)}
        </span>
      ),
    },
    {
      key: 'lineorder', name: 'Line-order check',
      tip: 'Always notes two-line entries that look swapped. With this on, a file with many swapped lines is flagged "fetch a fresh subtitle". Nothing is rewritten.',
      manual: s.line_order_enabled, setManual: (v: boolean) => sync.setData({ ...s, line_order_enabled: v }),
      auto: g.auto_scan_line_order_enabled, setAuto: (v: boolean) => general.setData({ ...g, auto_scan_line_order_enabled: v }),
      action: (
        <span style={{ display: 'flex', gap: 7, alignItems: 'center' }}>
          <span className="text-dim">flag only</span>
          {tipBtn('fixed', 'flag only', 'Swapped lines are never rewritten. Many of them make the file SUSPECT (action as set above); a few are only noted.')}
        </span>
      ),
    },
    ...(!GENERATE_UI ? [] : [{
      key: 'generate', name: 'Generate missing subtitles',
      tip: 'Transcribes the whole file with Whisper and translates it if needed, for a video that has no subtitle at all. Configured on the Generate tab.',
      manual: gen.enabled, setManual: (v: boolean) => generate.setData({ ...gen, enabled: v }),
      auto: g.auto_scan_generate_enabled, setAuto: (v: boolean) => general.setData({ ...g, auto_scan_generate_enabled: v }),
      action: <span className="text-faint">—</span>,
    }]),
  ]

  return (
    <>
      <h3 style={{ marginTop: 0, marginBottom: 4 }}>What runs</h3>
      <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 620, marginTop: 0, marginBottom: 14 }}>
        The Manual column is for a Scan/Rescan you click yourself. The next column is shared by
        the scheduled sweep and the Bazarr poll — both scan on their own without you asking, so
        they use the same switch.
      </p>
      <div role="table" aria-label="What runs" style={{ marginBottom: 16 }}>
        <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.2fr) 110px 220px minmax(0,1.4fr)', gap: 12, padding: '7px 0', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
          <div role="columnheader"></div>
          <div role="columnheader">Manual scan</div>
          <div role="columnheader">Scheduled sweep / Bazarr poll</div>
          <div role="columnheader">Action if SUSPECT</div>
        </div>
        {rows.map((r) => (
          <div key={r.key} role="row" data-row style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.2fr) 110px 220px minmax(0,1.4fr)', gap: 12, alignItems: 'center', padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
            <div role="cell" data-cell="main" style={{ fontWeight: 500 }}>
              {r.name}{tipBtn(r.key, r.name, r.tip)}
              {tipOpen === r.key && <div className="field-hint" style={{ color: 'var(--text-dim)', marginTop: 4, fontWeight: 400 }}>{r.tip}</div>}
              {r.key === 'correctness' && tipOpen === 'action' && <div className="field-hint" style={{ color: 'var(--text-dim)', marginTop: 4, fontWeight: 400 }}>{AUTO_ACTION_TIP}</div>}
            </div>
            <div role="cell" data-cell data-label="Manual scan">
              <input type="checkbox" aria-label={`${r.name}, manual scan`} checked={r.manual} onChange={(e) => r.setManual(e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)', margin: 0 }} />
            </div>
            <div role="cell" data-cell data-label="Scheduled sweep / Bazarr poll">
              <input type="checkbox" aria-label={`${r.name}, scheduled sweep and Bazarr poll`} checked={r.auto} onChange={(e) => r.setAuto(e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)', margin: 0 }} />
            </div>
            <div role="cell" data-cell data-label="Action if SUSPECT">{r.action}</div>
          </div>
        ))}
      </div>
    </>
  )
}

function GeneralTab() {
  const { data, setData, error: loadError, reload } = useGroup<GeneralSettings>('general')
  const { data: bazarrData } = useGroup<BazarrSettings>('bazarr')
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [posting, setPosting] = useState(false) // click sent, server not yet reporting running
  const postingRef = useRef(false)
  postingRef.current = posting
  const [detectProgress, setDetectProgress] = useState<{ done: number; total: number } | null>(null)
  const [detectResult, setDetectResult] = useState<string | null>(null)
  const [detectError, setDetectError] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [showBazarrWarning, setShowBazarrWarning] = useState(false)
  const { toast } = useToasts()
  const navigate = useNavigate()

  const pick = (d: GeneralSettings) => ({
    movies_folder: d.movies_folder,
    series_folder: d.series_folder,
    subtitle_langs: d.subtitle_langs,
    backup_originals: d.backup_originals,
  })

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(pick(data)))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(pick(data)) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      // Only this card's own fields — NOT auto_scan_* (Automation's "What runs" table owns
      // those, via its own separate fetch/save of the same "general" group).
      const r = await api.put<GeneralSettings>('/settings/general', { values: pick(data) })
      setData(r)
      setSnapshot(JSON.stringify(pick(r)))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, false, saving, save, discard)

  // Polls the actual server-side state rather than trusting only this component's own
  // `detecting` -- switching Settings tabs unmounts this component entirely, so on its own
  // that state can't survive a tab switch and back while a rescan is still running.
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const s = await api.get<{ running: boolean; done: number; total: number; cancelled: boolean }>(
          '/library/rescan/status',
        )
        if (cancelled) return
        setDetecting((d) => (postingRef.current ? d : s.running))
        setDetectProgress(s.running && s.total > 0 ? { done: s.done, total: s.total } : null)
      } catch {
        // transient poll failure -- try again next tick rather than showing an error for this
      }
      if (!cancelled) timer = setTimeout(poll, 1000)
    }
    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  async function detectNow() {
    setDetectResult(null)
    setDetectError(null)
    setDetecting(true)
    setPosting(true)
    try {
      const r = await api.post<LibraryResponse>('/library/rescan')
      setDetectResult(
        r.cancelled
          ? 'Stopped — nothing changed.'
          : `Found ${r.pairs_found ?? 0} video/subtitle pair(s)` +
              (r.missing_found ? `, ${r.missing_found} missing language(s)` : '') +
              ` — ${formatRelative(r.last_scanned_at)}.`,
      )
    } catch (err) {
      setDetectError(err instanceof ApiError ? err.message : String(err))
    }
    setPosting(false)
    // No `finally { setDetecting(false) }` here on purpose -- the poll loop above picks up
    // the real "running: false" from the server, so it stays correct even if this component
    // unmounted (tab switch) before this request resolved.
  }

  function handleDetectClick() {
    const bazarrConfigured = !!bazarrData?.url && !!bazarrData?.api_key.is_set
    if (bazarrConfigured) {
      detectNow()
    } else {
      setShowBazarrWarning(true)
    }
  }

  async function stopDetect() {
    setStopping(true)
    try {
      await api.post('/library/rescan/cancel')
    } catch (err) {
      setDetectError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setStopping(false)
    }
  }

  if (!data) return <span className="spinner" />

  return (
    <>
      {showBazarrWarning && (
        <ConfirmDialog
          title="Bazarr isn't configured"
          message="Without Bazarr set up (Settings -> Bazarr), embedded-subtitle detection has to check every video file individually instead of reading it from Bazarr in bulk -- much slower on a large library. You can still run it, or set up Bazarr first."
          confirmLabel="Detect anyway"
          onConfirm={() => {
            setShowBazarrWarning(false)
            detectNow()
          }}
          onCancel={() => setShowBazarrWarning(false)}
        />
      )}
      <section className="card" aria-labelledby="sc-general0">
        <h2 id="sc-general0" style={{ margin: '0 0 14px', fontSize: 15 }}>Media folders</h2>
        <SingleFolderField
          label="Movies folder"
          tip="Pick the folder Docker has mounted for movies -- see the volumes in docker-compose.yml."
          path={data.movies_folder}
          onChange={(movies_folder) => setData({ ...data, movies_folder })}
        />
        <SingleFolderField
          label="Series folder"
          tip="Same, but for TV shows."
          path={data.series_folder}
          onChange={(series_folder) => setData({ ...data, series_folder })}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
          {detecting ? (
            <button type="button" className="btn btn-sm" disabled={stopping} onClick={stopDetect}>
              {stopping ? <span className="spinner" /> : 'Stop'}
            </button>
          ) : (
            <button type="button" className="btn btn-sm" onClick={handleDetectClick} style={{ whiteSpace: 'nowrap', flex: 'none' }}>
              Detect now
            </button>
          )}
          {detecting && !detectProgress && (
            <span className="text-dim" style={{ fontSize: 12.5 }}>
              <span className="spinner" style={{ width: 11, height: 11, marginRight: 6 }} />
              Rescan started. Looking through the folders…
            </span>
          )}
          {detectProgress && (
            <span className="text-faint mono" style={{ fontSize: 12.5 }}>
              {detectProgress.done} / {detectProgress.total}
            </span>
          )}
          <span className="text-dim" style={{ fontSize: 12.5 }}>
            {detectError ?? detectResult ?? 'Rechecks the folders above and refreshes Movies/Series/Files right away, instead of waiting for the next automatic check or a full sweep.'}
          </span>
        </div>
      </section>

      <section className="card" aria-labelledby="sc-general1">
        <h2 id="sc-general1" style={{ margin: '0 0 10px', fontSize: 15 }}>Languages</h2>
        <p className="text-dim" style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>
          Only English subtitles are checked and fixed for now. Subtitles in other languages are left alone.
        </p>
      </section>

      <section className="card" aria-labelledby="sc-general2">
        <h2 id="sc-general2" style={{ margin: '0 0 14px', fontSize: 15 }}>Backups</h2>
        <ToggleRow
          id="backup_originals"
          checked={data.backup_originals}
          onChange={(backup_originals) => setData({ ...data, backup_originals })}
          label="Back up subtitles before overwriting them"
          tip="On by default. A copy is saved before any automatic edit overwrites a subtitle — the sync fix, a line-order swap, or a pre-blacklist removal. Worth keeping on: the line-order auto-fix runs at about 98% precision, so a small share of its swaps are wrong and this is the only way back. A failed backup never blocks the fix itself."
        />
      </section>

      <section className="card" aria-labelledby="h-wz" style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <h2 id="h-wz" style={{ margin: '0 0 2px', fontSize: 15 }}>Setup wizard</h2>
          <p className="text-dim" style={{ margin: 0, fontSize: 13 }}>Walk through folders, languages, Bazarr, speech recognition and schedule again.</p>
        </div>
        <button
          className="btn"
          onClick={() => {
            if (dirty) {
              toast('Save your changes first, or discard them — the wizard starts from what is saved.', { kind: 'warn' })
              return
            }
            navigate('/wizard?from=settings')
          }}
        >
          Run setup wizard
        </button>
      </section>
    </>
  )
}

function SyncTab() {
  const { data, setData, error: loadError, reload } = useGroup<SyncSettings>('sync')
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Everything except the two Automation-owned master switches.
  const pick = (d: SyncSettings) => {
    const { enabled: _e, line_order_enabled: _l, ...rest } = d
    return rest
  }

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(pick(data)))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(pick(data)) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      // NOT "enabled"/"line_order_enabled" — those switches live on the Automation tab and are
      // saved from its own copy of this group.
      const r = await api.put<SyncSettings>('/settings/sync', { values: pick(data) })
      setData(r)
      setSnapshot(JSON.stringify(pick(r)))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, true, saving, save, discard)

  if (!data) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-labelledby="sc-sync0">
        <h2 id="sc-sync0" style={{ margin: '0 0 14px', fontSize: 15 }}>How hard to look</h2>
        <ChoiceField
          label="Whisper mode"
          tip="Auto: the whole transcript with a tiny model (about 2 minutes per episode, and it sees block shifts that clips can miss: 99.3% against 96% in the test set), short clips with any bigger model. Sampled: short clips spread over the file (fast, cheap); anything suspicious still gets the whole transcript. Full transcript: transcribes the whole episode/movie every time -- the most thorough, but far more Whisper work per file (local Whisper: time; cloud: API cost/quota)."
          name="whisper_mode"
          value={data.whisper_mode}
          onPick={(whisper_mode) => setData({ ...data, whisper_mode })}
          options={[
            { value: 'auto', title: 'Auto (recommended)', desc: 'whole file with a tiny model, clips with bigger ones' },
            { value: 'sampled', title: 'Sampled clips', desc: 'short clips spread over the file' },
            { value: 'full', title: 'Full episode/movie transcript', desc: 'the whole file, every time' },
          ]}
        />
        {data.whisper_mode !== 'full' && (
          <Field
            label="Clips per 10 minutes"
            tip="How many 30s audio clips to check per 10 minutes of video (at least 3), placed where there is dialogue. Long films get proportionally more. More clips notice more on their own but cost more Whisper time. 0 = a fixed 16 clips per file."
          >
            <input type="number" step={0.5} min={0} value={data.clips_per_10min} onChange={(e) => setData({ ...data, clips_per_10min: Number(e.target.value) })} style={{ maxWidth: 120 }} />
          </Field>
        )}
        <Field advanced label="Min. change (seconds)" tip="Corrections smaller than this are not written. 0.25s is below what a viewer notices.">
          <input type="number" step={0.05} value={data.min_change_seconds} onChange={(e) => setData({ ...data, min_change_seconds: Number(e.target.value) })} style={{ maxWidth: 120 }} />
        </Field>
      </section>

      <Advanced>
        <section className="card" aria-labelledby="sc-sync1">
          <h2 id="sc-sync1" style={{ margin: '0 0 14px', fontSize: 15 }}>Framerate and drift</h2>
          <ToggleRow
            id="fps_check_enabled"
            checked={data.fps_check_enabled}
            onChange={(fps_check_enabled) => setData({ ...data, fps_check_enabled })}
            label="Fix framerate and speed errors"
            tip="Finds subtitles made for another framerate or speed (24 vs 23.976, PAL 25) that slowly walk out of sync, and rescales them. Only rewrites once the whole transcript and the audio's own speech pattern agree."
            advanced
          />
          {data.whisper_mode !== 'full' && (
            <Field
              label="Drift look-closer threshold (s)"
              tip="If the clips drift apart by at least this much from the start to the end of the file, the whole file is transcribed to check for a framerate/speed error. It never fixes anything on its own. Lower catches smaller drift but transcribes more healthy files. Measured: real 0.1% drift 0.8-1.7s, healthy files up to 0.9s. 0 = off."
              advanced
            >
              <input type="number" step={0.1} min={0} disabled={!data.fps_check_enabled} value={data.clip_tilt_escalate_s} onChange={(e) => setData({ ...data, clip_tilt_escalate_s: Number(e.target.value) })} style={{ maxWidth: 120 }} />
            </Field>
          )}
        </section>

        <section className="card" aria-labelledby="sc-sync2">
          <h2 id="sc-sync2" style={{ margin: '0 0 14px', fontSize: 15 }}>Blocks and offsets</h2>
          {data.whisper_mode !== 'full' && (
            <ToggleRow
              id="escalate_sampled_to_full"
              checked={data.escalate_sampled_to_full}
              onChange={(escalate_sampled_to_full) => setData({ ...data, escalate_sampled_to_full })}
              label="Transcribe the whole file when the clips see a problem"
              tip="Clips that disagree, a possible block, noisy timing, many swapped lines or lines after the audio ends: the whole file is transcribed before deciding. Off saves Whisper time on those files, but blocks between clips go unnoticed and fewer can be repaired. The framerate check above has its own switch."
              advanced
            />
          )}
          <ToggleRow
            id="anchor_check_enabled"
            checked={data.anchor_check_enabled}
            onChange={(anchor_check_enabled) => setData({ ...data, anchor_check_enabled })}
            label="Whisper anchor check"
            tip="Flags a file when Whisper-verified lines show a timing mismatch of more than ~2.5s, even though the text matches overall. Only works for subtitles in the spoken language."
            advanced
          />
          <ToggleRow
            id="anchor_resync_enabled"
            checked={data.anchor_resync_enabled}
            onChange={(anchor_resync_enabled) => setData({ ...data, anchor_resync_enabled })}
            label="Re-sync from the anchors instead of only flagging"
            tip="When the anchors show a file is mis-timed, they also measured by how much -- so the file is corrected, stretch by stretch, instead of just reported. The result is re-measured against the audio before anything is written. Needs the anchor check above."
            advanced
            disabled={!data.anchor_check_enabled}
          />
        </section>
      </Advanced>
    </>
  )
}

function CorrectnessTab() {
  const { data, setData, error: loadError, reload } = useGroup<CorrectnessSettings>('correctness')
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  // NOT "enabled"/"auto_action" — Automation's "What runs" table owns those, from its own
  // copy of this group.
  const pick = (d: CorrectnessSettings) => {
    const { enabled: _e, auto_action: _a, ...rest } = d
    return rest
  }

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(pick(data)))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(pick(data)) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      const r = await api.put<CorrectnessSettings>('/settings/correctness', { values: pick(data) })
      setData(r)
      setSnapshot(JSON.stringify(pick(r)))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, true, saving, save, discard)

  if (!data) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-labelledby="sc-corr-act">
        <h2 id="sc-corr-act" style={{ margin: '0 0 14px', fontSize: 15 }}>What a flag does</h2>
        <ToggleRow
          id="act_on_missing_lines"
          checked={data.act_on_missing_lines}
          onChange={(act_on_missing_lines) => setData({ ...data, act_on_missing_lines })}
          label="Act on missing lines"
          tip="A stretch of speech with no subtitle lines is always shown as a flag with the time span. Off (default): that flag only informs. In practice most are songs, background chatter or burned-in subtitles, and the file is fine. On: it triggers the action chosen under Automation (quarantine, blacklist or fetch a new one) like any other suspect file."
        />
      </section>

      <section className="card" aria-labelledby="sc-corr0">
        <h2 id="sc-corr0" style={{ margin: '0 0 4px', fontSize: 15 }}>Whisper (local)</h2>
        <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 560, margin: '0 0 14px', lineHeight: 1.5 }}>
          Turned on/off from Settings → Automation → What runs. The checks always listen with
          whisper.cpp on this machine — no cloud speech recognition and no API key.
        </p>
        <GpuStatus />
        <Field advanced label="Model file path" tip="A ggml model file. Every threshold is measured on tiny.en (the default) -- other models transcribe differently and are not calibrated.">
          <input type="text" value={data.local_whisper_model} onChange={(e) => setData({ ...data, local_whisper_model: e.target.value })} />
        </Field>
        <Field advanced label="Binary path" tip="The whisper.cpp binary. Ships at /usr/local/bin/whisper-cli in Docker.">
          <input type="text" value={data.local_whisper_binary} onChange={(e) => setData({ ...data, local_whisper_binary: e.target.value })} />
        </Field>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, maxWidth: 540 }}>
          <Field label="Use GPU" tip="Off forces CPU-only (-ng) even if the binary was built with Vulkan/GPU support.">
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, margin: 0 }}>
              <input type="checkbox" checked={data.local_whisper_use_gpu} onChange={(e) => setData({ ...data, local_whisper_use_gpu: e.target.checked })} style={{ width: 16, height: 16, accentColor: 'var(--accent)' }} />
            </label>
          </Field>
          <Field label="CPU threads" tip="Threads for local Whisper. 0 uses every core it is allowed to run on.">
            <input type="number" min={0} value={data.local_whisper_threads} onChange={(e) => setData({ ...data, local_whisper_threads: Number(e.target.value) })} />
          </Field>
          <Field label="CPU cores" tip="Which cores Whisper may use, e.g. 0-3,6. Empty = all cores. Leave a few free to keep the rest of the machine responsive.">
            <input type="text" placeholder="all" value={data.local_whisper_cpus} onChange={(e) => setData({ ...data, local_whisper_cpus: e.target.value })} />
          </Field>
        </div>
      </section>

      <section className="card" aria-labelledby="sc-corr2">
        <h2 id="sc-corr2" style={{ margin: '0 0 14px', fontSize: 15 }}>Checking</h2>
        <Field advanced label="Required audio language" tip="Files whose audio track is another language are skipped. Timing anchors need the subtitle in the spoken language. Empty = run regardless.">
          <input type="text" value={data.require_audio_lang} onChange={(e) => setData({ ...data, require_audio_lang: e.target.value })} />
        </Field>
      </section>
    </>
  )
}

function GenerateTab() {
  const { data, setData, error: loadError, reload } = useGroup<GenerateSettings>('generate')
  const [newKeys, setNewKeys] = useState({ groq: '', openrouter: '', cloudflare: '', gemini: '' })
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const pick = (d: GenerateSettings, nk: typeof newKeys) => {
    const { groq_api_key: _g, openrouter_api_key: _o, cloudflare_api_token: _c, gemini_api_key: _m, enabled: _e, ...rest } = d
    return { ...rest, newKeys: nk }
  }

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(pick(data, { groq: '', openrouter: '', cloudflare: '', gemini: '' })))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(pick(data, newKeys)) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      // NOT "enabled" (Automation owns that switch), and secrets only when re-typed.
      const { newKeys: _n, ...values } = pick(data, { groq: '', openrouter: '', cloudflare: '', gemini: '' }) as Record<string, unknown>
      if (newKeys.groq) values.groq_api_key = newKeys.groq
      if (newKeys.openrouter) values.openrouter_api_key = newKeys.openrouter
      if (newKeys.cloudflare) values.cloudflare_api_token = newKeys.cloudflare
      if (newKeys.gemini) values.gemini_api_key = newKeys.gemini
      const r = await api.put<GenerateSettings>('/settings/generate', { values })
      setData(r)
      setNewKeys({ groq: '', openrouter: '', cloudflare: '', gemini: '' })
      setSnapshot(JSON.stringify(pick(r, { groq: '', openrouter: '', cloudflare: '', gemini: '' })))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setNewKeys({ groq: '', openrouter: '', cloudflare: '', gemini: '' })
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, true, saving, save, discard)

  if (!data) return <span className="spinner" />

  const apiKeyField = (which: keyof typeof newKeys, label: string, isSet: boolean, placeholder: string) => (
    <Field label={label} tip={isSet ? 'A key is already saved — type here only to replace it.' : 'Not set yet.'}>
      <input
        type="text"
        autoComplete="off"
        placeholder={isSet ? '••••••••••••••••  (saved — leave blank to keep)' : placeholder}
        value={newKeys[which]}
        onChange={(e) => setNewKeys({ ...newKeys, [which]: e.target.value })}
      />
    </Field>
  )

  return (
    <>
      <section className="card" aria-labelledby="sc-gen0">
        <h2 id="sc-gen0" style={{ margin: '0 0 4px', fontSize: 15 }}>Speech-to-text</h2>
        <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 560, margin: '0 0 14px', lineHeight: 1.5 }}>
          Generates a subtitle from scratch (via Whisper) for a video that has no subtitle at all,
          then translates it into any other wanted language with an LLM. Turned on/off from
          Settings → Automation → What runs — the fields below configure the providers it uses once
          it&apos;s on. These are the only cloud keys in the app: the correctness check listens locally
          and only borrows the translation model below for subtitles in another language.
        </p>
        <ChoiceField
          label="Provider"
          name="gen-stt"
          value={data.stt_provider}
          onPick={(stt_provider) => setData({ ...data, stt_provider })}
          options={[
            { value: 'groq', title: 'Groq', desc: 'fast Whisper API' },
            { value: 'openrouter', title: 'OpenRouter', desc: 'Whisper via OpenRouter' },
            { value: 'cloudflare', title: 'Cloudflare Workers AI', desc: 'very low cost' },
          ]}
        />
        {data.stt_provider === 'groq' && (
          <>
            {apiKeyField('groq', 'Groq API key', data.groq_api_key.is_set, 'gsk_…')}
            <Field advanced label="Whisper model">
              <input type="text" value={data.groq_stt_model} onChange={(e) => setData({ ...data, groq_stt_model: e.target.value })} />
            </Field>
          </>
        )}
        {data.stt_provider === 'openrouter' && (
          <>
            {apiKeyField('openrouter', 'OpenRouter API key', data.openrouter_api_key.is_set, 'sk-or-…')}
            <Field advanced label="Whisper model">
              <input type="text" value={data.openrouter_stt_model} onChange={(e) => setData({ ...data, openrouter_stt_model: e.target.value })} />
            </Field>
          </>
        )}
        {data.stt_provider === 'cloudflare' && (
          <>
            <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 560, marginTop: 0 }}>
              Cloudflare doesn&apos;t publish a per-request audio size/duration limit for this model —
              start with a short chunk length and only raise it after confirming longer chunks
              actually succeed against your own account.
            </p>
            <Field label="Account ID">
              <input type="text" value={data.cloudflare_account_id} onChange={(e) => setData({ ...data, cloudflare_account_id: e.target.value })} />
            </Field>
            <Field
              label="API token"
              tip={data.cloudflare_api_token.is_set ? 'A token is already saved — type here only to replace it.' : 'Not set yet.'}
            >
              <input
                type="text"
                autoComplete="off"
                placeholder={data.cloudflare_api_token.is_set ? '••••••••••••••••  (saved — leave blank to keep)' : 'Workers AI API token'}
                value={newKeys.cloudflare}
                onChange={(e) => setNewKeys({ ...newKeys, cloudflare: e.target.value })}
              />
            </Field>
            <Field advanced label="Whisper model">
              <input type="text" value={data.cloudflare_stt_model} onChange={(e) => setData({ ...data, cloudflare_stt_model: e.target.value })} />
            </Field>
            <Field advanced label="Chunk length (seconds)" tip="Undocumented limit — tune against your own account (see note above).">
              <input type="number" step={10} min={10} value={data.chunk_seconds_cloudflare} onChange={(e) => setData({ ...data, chunk_seconds_cloudflare: Number(e.target.value) })} style={{ maxWidth: 120 }} />
            </Field>
          </>
        )}
        <Field advanced
          label="Assume spoken language"
          tip="Fallback when neither the provider nor the file's own audio-language tag can tell us what's spoken. Leave empty to skip a video rather than guess."
        >
          <input type="text" placeholder="e.g. en" value={data.assume_spoken_lang} onChange={(e) => setData({ ...data, assume_spoken_lang: e.target.value })} />
        </Field>
        <Field advanced
          label="Vocabulary hint (Groq/OpenRouter only)"
          tip="A short, plain comma-separated list of names Whisper is likely to mishear (e.g. show/character names) -- helps with proper nouns. Keep it a plain list, not a labeled sentence ('Characters: ...') -- that shape was observed to make Whisper hallucinate extra dialogue near the end of a chunk. Saved as a single line, capped at 200 characters."
        >
          <input
            type="text"
            placeholder="e.g. Jeff, Britta, Abed, Troy, Annie, Shirley, Pierce, Chang"
            maxLength={200}
            value={data.vocabulary_hint}
            onChange={(e) => setData({ ...data, vocabulary_hint: e.target.value })}
          />
        </Field>
      </section>

      <section className="card" aria-labelledby="sc-gen1">
        <h2 id="sc-gen1" style={{ margin: '0 0 4px', fontSize: 15 }}>Translation</h2>
        <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 560, margin: '0 0 14px', lineHeight: 1.5 }}>
          Whisper can only translate speech straight to English — any OTHER wanted language goes
          through this LLM step instead, translating the already-timed lines without touching their
          timestamps. The correctness check also uses it to compare a subtitle in another language
          with the English audio.
        </p>
        <ChoiceField
          label="Provider"
          name="gen-llm"
          value={data.llm_provider}
          onPick={(llm_provider) => setData({ ...data, llm_provider })}
          options={[
            { value: 'groq', title: 'Groq', desc: 'Llama chat models' },
            { value: 'openrouter', title: 'OpenRouter', desc: 'model of your choice' },
            { value: 'gemini', title: 'Google Gemini', desc: "Google's models" },
          ]}
        />
        {data.llm_provider === 'groq' && (
          <>
            {data.stt_provider !== 'groq' && apiKeyField('groq', 'Groq API key', data.groq_api_key.is_set, 'gsk_…')}
            <Field advanced label="Translation model">
              <input type="text" value={data.groq_llm_model} onChange={(e) => setData({ ...data, groq_llm_model: e.target.value })} />
            </Field>
          </>
        )}
        {data.llm_provider === 'openrouter' && (
          <>
            {data.stt_provider !== 'openrouter' && apiKeyField('openrouter', 'OpenRouter API key', data.openrouter_api_key.is_set, 'sk-or-…')}
            <Field advanced label="Translation model">
              <input type="text" value={data.openrouter_llm_model} onChange={(e) => setData({ ...data, openrouter_llm_model: e.target.value })} />
            </Field>
          </>
        )}
        {data.llm_provider === 'gemini' && (
          <>
            {apiKeyField('gemini', 'Gemini API key', data.gemini_api_key.is_set, 'AIza…')}
            <Field advanced label="Translation model">
              <input type="text" value={data.gemini_llm_model} onChange={(e) => setData({ ...data, gemini_llm_model: e.target.value })} />
            </Field>
          </>
        )}
      </section>

      <section className="card" aria-labelledby="sc-gen2">
        <h2 id="sc-gen2" style={{ margin: '0 0 14px', fontSize: 15 }}>Limits</h2>
        <Field
          label="Max. videos per day"
          tip="Caps how many DISTINCT videos get a subtitle generated in any 24 hours -- counted across every sweep, poll and scheduled run together, so a busy day can't quietly multiply it. Translating an already-transcribed video into extra languages doesn't count again. A manual Generate click ignores this."
        >
          <input type="number" step={1} min={0} value={data.max_videos_per_day} onChange={(e) => setData({ ...data, max_videos_per_day: Number(e.target.value) })} style={{ maxWidth: 120 }} />
        </Field>
      </section>
    </>
  )
}

function AutomationTab() {
  const [automation, setAutomation] = useState<AutomationSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wr = useWhatRuns()

  useEffect(() => {
    api
      .get<AutomationSettings>('/settings/automation')
      .then(setAutomation)
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : String(err)))
  }, [])

  const ready = !!(automation && wr.general.data && wr.sync.data && wr.correctness.data && wr.generate.data)
  const pickNow = () => JSON.stringify({
    a: automation,
    g: wr.general.data ? {
      auto_scan_sync_enabled: wr.general.data.auto_scan_sync_enabled,
      auto_scan_correctness_enabled: wr.general.data.auto_scan_correctness_enabled,
      auto_scan_line_order_enabled: wr.general.data.auto_scan_line_order_enabled,
      auto_scan_generate_enabled: wr.general.data.auto_scan_generate_enabled,
    } : null,
    s: wr.sync.data ? { enabled: wr.sync.data.enabled, line_order_enabled: wr.sync.data.line_order_enabled } : null,
    c: wr.correctness.data ? { enabled: wr.correctness.data.enabled, auto_action: wr.correctness.data.auto_action } : null,
    gen: wr.generate.data ? { enabled: wr.generate.data.enabled } : null,
  })

  // Guarded: only snapshots once per load, so the missing dep array can't loop.
  useEffect(() => {
    if (ready && snapshot === null) setSnapshot(pickNow())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  })
  const dirty = ready && snapshot !== null && pickNow() !== snapshot

  async function save() {
    const g = wr.general.data, s = wr.sync.data, c = wr.correctness.data, gen = wr.generate.data
    if (!automation || !g || !s || !c || !gen) return
    setSaving(true)
    setError(null)
    try {
      // Each switch is a key in its owning group; AutomationTab is the only writer of these
      // particular keys, so no tab can overwrite another's change here.
      const [ra, rg, rs, rc, rgen] = await Promise.all([
        api.put<AutomationSettings>('/settings/automation', { values: { ...automation } }),
        api.put<GeneralSettings>('/settings/general', {
          values: {
            auto_scan_sync_enabled: g.auto_scan_sync_enabled,
            auto_scan_correctness_enabled: g.auto_scan_correctness_enabled,
            auto_scan_line_order_enabled: g.auto_scan_line_order_enabled,
            auto_scan_generate_enabled: g.auto_scan_generate_enabled,
          },
        }),
        api.put<SyncSettings>('/settings/sync', { values: { enabled: s.enabled, line_order_enabled: s.line_order_enabled } }),
        api.put<CorrectnessSettings>('/settings/correctness', { values: { enabled: c.enabled, auto_action: c.auto_action } }),
        api.put<GenerateSettings>('/settings/generate', { values: { enabled: gen.enabled } }),
      ])
      setAutomation(ra)
      wr.general.setData(rg)
      wr.sync.setData(rs)
      wr.correctness.setData(rc)
      wr.generate.setData(rgen)
      setSnapshot(JSON.stringify({
        a: ra,
        g: {
          auto_scan_sync_enabled: rg.auto_scan_sync_enabled,
          auto_scan_correctness_enabled: rg.auto_scan_correctness_enabled,
          auto_scan_line_order_enabled: rg.auto_scan_line_order_enabled,
          auto_scan_generate_enabled: rg.auto_scan_generate_enabled,
        },
        s: { enabled: rs.enabled, line_order_enabled: rs.line_order_enabled },
        c: { enabled: rc.enabled, auto_action: rc.auto_action },
        gen: { enabled: rgen.enabled },
      }))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setSnapshot(null)
    setError(null)
    setAutomation(null)
    wr.general.reload()
    wr.sync.reload()
    wr.correctness.reload()
    wr.generate.reload()
    api
      .get<AutomationSettings>('/settings/automation')
      .then(setAutomation)
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : String(err)))
  }

  useToolbarApi(dirty, error ?? loadError ?? wr.loadError, savedAt, true, saving, save, discard)

  if (!automation) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-label="Automation">
        <WhatRunsTable {...wr} />
        <Field
          label="Max. remediation attempts"
          tip="How many replacement subtitles to try from Bazarr's providers. If none of them passes the check, the original is put back and stays flagged."
        >
          <input type="number" value={automation.remediate_max_attempts} onChange={(e) => setAutomation({ ...automation, remediate_max_attempts: Number(e.target.value) })} style={{ maxWidth: 120 }} />
        </Field>
        <Field advanced
          label="Minimum Bazarr score to try (remediation, %)"
          tip="Bazarr's own match score for a candidate -- one below this is skipped during remediation. 0 = try everything."
        >
          <input type="number" step={1} min={0} max={100} value={automation.remediate_min_score} onChange={(e) => setAutomation({ ...automation, remediate_min_score: Number(e.target.value) })} style={{ maxWidth: 120 }} />
        </Field>
        <ToggleRow
          id="dry_run"
          checked={automation.dry_run}
          onChange={(dry_run) => setAutomation({ ...automation, dry_run })}
          label="Dry run — show what would happen, don't change anything"
        />
      </section>
    </>
  )
}

function BazarrTab() {
  const { data, setData, error: loadError, reload } = useGroup<BazarrSettings>('bazarr')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('')
  const [newApiKey, setNewApiKey] = useState('')
  const [initialized, setInitialized] = useState(false)
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<string | null>(null)
  const [testError, setTestError] = useState<string | null>(null)

  useEffect(() => {
    if (data && !initialized) {
      const { host: h, port: p } = urlToHostPort(data.url)
      setHost(h)
      setPort(p)
      setInitialized(true)
    }
  }, [data, initialized])

  const pickNow = () => data ? JSON.stringify({ host, port, path_map: data.path_map, newApiKey }) : ''
  // Guarded: only snapshots once per load, so the missing dep array can't loop.
  useEffect(() => {
    if (data && initialized && snapshot === null) setSnapshot(pickNow())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  })
  const dirty = data !== null && initialized && snapshot !== null && pickNow() !== snapshot

  function pathMapToText(pairs: [string, string][]) {
    return pairs.map(([a, b]) => `${a}=${b}`).join('\n')
  }
  function textToPathMap(text: string): [string, string][] {
    return text
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .filter((l) => l.includes('='))
      .map((l) => {
        const [a, b] = l.split('=')
        return [a.trim(), b.trim()] as [string, string]
      })
  }

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      const values: Record<string, unknown> = { url: hostPortToUrl(host, port), path_map: data.path_map }
      // Only when the user typed a new one — otherwise the server's stored key survives.
      if (newApiKey) values.api_key = newApiKey
      const r = await api.put<BazarrSettings>('/settings/bazarr', { values })
      setData(r)
      setNewApiKey('')
      const { host: h, port: p } = urlToHostPort(r.url)
      setHost(h)
      setPort(p)
      setSnapshot(JSON.stringify({ host: h, port: p, path_map: r.path_map, newApiKey: '' }))
      setTestResult(null)
      setTestError(null)
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setNewApiKey('')
    setInitialized(false)
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, false, saving, save, discard)

  async function test() {
    setTesting(true)
    setTestResult(null)
    setTestError(null)
    try {
      // Tests what's in the form RIGHT NOW, without requiring Save first — url is always
      // sent (so a field change is tested immediately); api_key only if the user typed a
      // new one (otherwise the already-saved one is used, see backend).
      const r = await api.post<{ ok: boolean; bazarr_version: string | null }>('/settings/bazarr/test-connection', {
        url: hostPortToUrl(host, port),
        api_key: newApiKey || undefined,
      })
      setTestResult(r.ok ? `Connected — Bazarr version ${r.bazarr_version ?? 'unknown'}` : 'Failed')
    } catch (err) {
      setTestError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setTesting(false)
    }
  }

  if (!data) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-labelledby="sc-bz0">
        <h2 id="sc-bz0" style={{ margin: '0 0 4px', fontSize: 15 }}>Connection</h2>
        <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 560, margin: '0 0 14px', lineHeight: 1.5 }}>
          Bazarr is where subtitles actually get downloaded from. Connecting it lets verifyarr look
          up where a subtitle came from (needed to blacklist a bad one) and ask Bazarr for a
          replacement during remediation. Optional — sync and correctness checking both work fine
          without it.
        </p>
        <HostPortFields host={host} port={port} onHost={setHost} onPort={setPort} />
        <Field
          label="Bazarr API key"
          tip={data.api_key.is_set ? "A key is already saved. It's left blank here on purpose -- type only if you want to replace it." : 'Not set yet.'}
        >
          <input
            type="text"
            autoComplete="off"
            placeholder={data.api_key.is_set ? '••••••••••••••••  (saved — leave blank to keep)' : 'Not set'}
            value={newApiKey}
            onChange={(e) => setNewApiKey(e.target.value)}
          />
        </Field>
        <Field
          label="Path mapping (PATH_MAP)"
          tip="Only needed if Bazarr and verifyarr see the media folders under different paths. Format: local-path=bazarr-path, one per line."
        >
          <textarea
            rows={3}
            value={pathMapToText(data.path_map)}
            onChange={(e) => setData({ ...data, path_map: textToPathMap(e.target.value) })}
            style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}
          />
        </Field>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
          <button type="button" className="btn btn-sm" disabled={testing} onClick={test}>
            {testing ? <span className="spinner" /> : 'Test connection'}
          </button>
          <span role="status" style={{ fontSize: 13 }}>
            {testResult && <span style={{ color: 'var(--green)' }}>✓ {testResult}</span>}
            {testError && <span style={{ color: 'var(--red)' }}>{testError}</span>}
          </span>
        </div>
      </section>
    </>
  )
}

function SchedulingTab() {
  const { data, setData, error: loadError, reload } = useGroup<SchedulingSettings>('scheduling')
  const [schedule, setSchedule] = useState<FriendlySchedule | null>(null)
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [serverTz, setServerTz] = useState<string | null>(null)
  const initialized = useRef(false)

  useEffect(() => {
    if (data && !initialized.current) {
      setSchedule(parseCron(data.cron))
      initialized.current = true
    }
  }, [data])

  useEffect(() => {
    api.get<NextRunResponse>('/runs/next').then(
      (r) => setServerTz(r.timezone ?? null),
      () => {},
    )
  }, [])

  const tzNote = `Local time on the server${serverTz ? ` (${serverTz})` : ''}.`

  function updateSchedule(patch: Partial<FriendlySchedule>) {
    const next = { ...schedule!, ...patch }
    setSchedule(next)
    if (next.mode !== 'advanced') {
      setData({ ...data!, cron: buildCron({ mode: next.mode, time: next.time, dayOfWeek: next.dayOfWeek }) })
    }
  }

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(data))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(data) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      const r = await api.put<SchedulingSettings>('/settings/scheduling', { values: { ...data } })
      setData(r)
      setSnapshot(JSON.stringify(r))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    initialized.current = false
    setSchedule(null)
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, false, saving, save, discard)

  if (!data || !schedule) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-labelledby="sc-sch0">
        <h2 id="sc-sch0" style={{ margin: '0 0 14px', fontSize: 15 }}>Scheduled sweep</h2>
        <Field label="Run a sweep">
          <select value={schedule.mode} onChange={(e) => updateSchedule({ mode: e.target.value as ScheduleMode })}>
            <option value="daily">Every day</option>
            <option value="weekly">Every week</option>
            <option value="advanced">Advanced (raw cron)</option>
          </select>
        </Field>
        {schedule.mode !== 'advanced' && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, maxWidth: 540 }}>
            {schedule.mode === 'weekly' && (
              <Field label="On">
                <select value={schedule.dayOfWeek} onChange={(e) => updateSchedule({ dayOfWeek: Number(e.target.value) })}>
                  {DAY_NAMES.map((name, i) => (
                    <option key={name} value={i}>{name}</option>
                  ))}
                </select>
              </Field>
            )}
            <Field label="At" tip={tzNote}>
              <TimeOfDayField value={schedule.time} onChange={(time) => updateSchedule({ time })} />
            </Field>
          </div>
        )}
        {schedule.mode === 'advanced' && (
          <Field label="Cron expression" tip={`Standard 5-field cron. ${tzNote} E.g. '0 4 * * 0' = Sunday at 04:00.`}>
            <input type="text" value={data.cron} onChange={(e) => setData({ ...data, cron: e.target.value })} style={{ fontFamily: 'var(--mono)' }} />
          </Field>
        )}
        <ToggleRow
          id="run_on_start"
          checked={data.run_on_start}
          onChange={(run_on_start) => setData({ ...data, run_on_start })}
          label="Also run a sweep immediately when the container starts"
        />
      </section>

      <section className="card" aria-labelledby="sc-sch1">
        <h2 id="sc-sch1" style={{ margin: '0 0 14px', fontSize: 15 }}>Background checks</h2>
        <ToggleRow
          id="poll_new_media_enabled"
          checked={data.poll_new_media_enabled}
          onChange={(poll_new_media_enabled) => setData({ ...data, poll_new_media_enabled })}
          label="Scan when Bazarr has a subtitle ready"
          tip="Scans an item as soon as Bazarr's satisfied it (needs a URL + API key on Settings → Bazarr). What the scan does is set under Automation → What runs."
        />
        {data.poll_new_media_enabled && (
          <Field label="Check every (minutes)" tip="How often to poll Bazarr's wanted-subtitles lists. While Verifyarr waits for a replacement it asked Bazarr for, it checks every 3 minutes instead.">
            <input type="number" min={1} value={data.poll_new_media_interval_minutes} onChange={(e) => setData({ ...data, poll_new_media_interval_minutes: Number(e.target.value) })} style={{ maxWidth: 120 }} />
          </Field>
        )}
        <ToggleRow
          id="poll_library_enabled"
          checked={data.poll_library_enabled}
          onChange={(poll_library_enabled) => setData({ ...data, poll_library_enabled })}
          label="Watch media folders for new files"
          tip="Rechecks the folders for new files and refreshes the Library page. Discovery only -- no sync, no correctness check."
        />
        {data.poll_library_enabled && (
          <Field label="Check every (hours)" tip="How often to re-walk the media folders for new/removed files. Stored as minutes under the hood -- fractional hours (e.g. 0.5) are fine.">
            <input
              type="number"
              min={0.1}
              step={0.5}
              value={data.poll_library_interval_minutes / 60}
              onChange={(e) => setData({ ...data, poll_library_interval_minutes: Math.round(Number(e.target.value) * 60) })}
              style={{ maxWidth: 120 }}
            />
          </Field>
        )}
      </section>
    </>
  )
}

const LOG_POLL_MS = 3000
const LOG_MAX_LINES = 2000

function LogTab() {
  const { data, setData, error: loadError, reload } = useGroup<LogSettings>('log')
  const [snapshot, setSnapshot] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lines, setLines] = useState<AppLogLine[]>([])
  const [viewerError, setViewerError] = useState<string | null>(null)
  const { ref: logBoxRef, onScroll: onLogScroll } = useAutoScrollLog(lines)
  const lastIdRef = useRef(0)

  useEffect(() => {
    if (data && snapshot === null) setSnapshot(JSON.stringify(data))
  }, [data, snapshot])
  const dirty = data !== null && snapshot !== null && JSON.stringify(data) !== snapshot

  async function save() {
    if (!data) return
    setSaving(true)
    setError(null)
    try {
      const r = await api.put<LogSettings>('/settings/log', { values: { ...data } })
      setData(r)
      setSnapshot(JSON.stringify(r))
      setSavedAt(Date.now())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setSnapshot(null)
    setError(null)
    reload()
  }

  useToolbarApi(dirty, error ?? loadError, savedAt, false, saving, save, discard)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const r = await api.get<{ items: AppLogLine[] }>(`/logs?after_id=${lastIdRef.current}&limit=500`)
        if (cancelled || r.items.length === 0) return
        lastIdRef.current = r.items[r.items.length - 1].id
        setLines((prev) => [...prev, ...r.items].slice(-LOG_MAX_LINES))
        setViewerError(null)
      } catch (err) {
        if (!cancelled) setViewerError(err instanceof ApiError ? err.message : String(err))
      }
    }
    poll()
    const id = setInterval(poll, LOG_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (!data) return <span className="spinner" />

  return (
    <>
      <section className="card" aria-labelledby="sc-log0">
        <h2 id="sc-log0" style={{ margin: '0 0 14px', fontSize: 15 }}>Detail</h2>
        <Field
          label="Log level"
          tip="How much detail gets logged. DEBUG is noisy -- only useful when chasing a specific problem."
        >
          <select value={data.level} onChange={(e) => setData({ ...data, level: e.target.value })}>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
        </Field>
      </section>

      <section className="card" aria-labelledby="sc-log1" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ padding: '12px 16px 10px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10 }}>
            <h2 id="sc-log1" style={{ margin: 0, fontSize: 15 }}>Recent log</h2>
            <CopyLogButton lines={lines} />
          </div>
          <p className="text-dim" style={{ fontSize: 12.5, margin: '4px 0 0', lineHeight: 1.5 }}>
            Updates on its own every few seconds. This is the whole app&apos;s log, not just one run —
            check here if something looks off and Activity doesn&apos;t explain why.
          </p>
        </div>
        {viewerError && <div className="error-banner" style={{ margin: '12px 16px 0' }}>{viewerError}</div>}
        <div ref={logBoxRef} onScroll={onLogScroll} tabIndex={0} aria-label="Application log" role="log" style={{ height: 420, overflow: 'auto', background: 'var(--bg)', padding: '8px 0', fontFamily: 'var(--mono)', fontSize: 12.5, lineHeight: 1.6 }}>
          {lines.length === 0 && <div className="text-faint" style={{ padding: '0 14px' }}>No log lines yet.</div>}
          {lines.map((l) => (
            <div key={l.id} style={{ display: 'grid', gridTemplateColumns: '70px 52px minmax(0,1fr)', gap: 10, padding: '0 14px' }}>
              <span className="text-dim">{new Date(l.ts).toLocaleTimeString('en-US')}</span>
              {l.level === 'WARN' ? (
                <span style={{ color: 'var(--yellow)', fontWeight: 600 }}>{l.level}</span>
              ) : l.level === 'ERROR' ? (
                <span style={{ color: 'var(--red)', fontWeight: 600 }}>{l.level}</span>
              ) : (
                <span className="text-dim" style={{ fontWeight: 600 }}>{l.level}</span>
              )}
              <span className={l.level === 'DEBUG' ? 'text-dim' : ''} style={{ wordBreak: 'break-word' }}>{l.message}</span>
            </div>
          ))}
        </div>
      </section>
    </>
  )
}

// Must match verifyarr.auth.MIN_PASSWORD_LENGTH (the server enforces it too).
const MIN_PASSWORD_LENGTH = 5

function AccountTab() {
  const { status } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [repeat, setRepeat] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useToolbarApi(false, null, null, false, false, () => {}, () => {})

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSaved(false)
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(`New password must be at least ${MIN_PASSWORD_LENGTH} characters`)
      return
    }
    if (next !== repeat) {
      setError('Passwords do not match')
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/change-password', { current_password: current, new_password: next })
      setCurrent('')
      setNext('')
      setRepeat('')
      setSaved(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={onSubmit} aria-labelledby="sc-acc0">
      <h2 id="sc-acc0" style={{ margin: '0 0 14px', fontSize: 15 }}>Account</h2>
      <div className="field" style={{ maxWidth: 540 }}>
        <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 500, marginBottom: 5 }}>Username</span>
        <div style={{ color: 'var(--text-dim)' }}>{status?.username ?? '—'}</div>
      </div>
      <Field label="Current password">
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field label="New password">
        <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        <div className="field-hint" style={{ color: 'var(--text-dim)' }}>At least {MIN_PASSWORD_LENGTH} characters.</div>
      </Field>
      <Field label="Repeat new password">
        <input type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} />
      </Field>
      {error && <div className="error-banner" role="alert" style={{ maxWidth: 540 }}>{error}</div>}
      {saved && <div style={{ fontSize: 13, color: 'var(--green)', marginBottom: 14 }}>✓ Password changed.</div>}
      <button className="btn btn-primary" type="submit" disabled={busy} style={{ marginTop: 4 }}>
        {busy ? <span className="spinner" /> : 'Change password'}
      </button>
    </form>
  )
}

const TAB_KEYS = SETTINGS_TABS.map((t) => t.key)

function GpuStatus() {
  const [st, setSt] = useState<{ checked: boolean; gpu: boolean; detail: string } | null>(null)
  useEffect(() => {
    api.get<{ checked: boolean; gpu: boolean; detail: string }>('/settings/whisper-status').then(setSt, () => setSt(null))
  }, [])
  if (!st) return null
  return (
    <p style={{ fontSize: 13, margin: '0 0 14px', lineHeight: 1.5 }} className={st.gpu ? '' : 'text-dim'}>
      <strong>GPU:</strong> {st.checked ? st.detail : 'checking…'}
    </p>
  )
}

export default function Settings() {
  const { tab } = useParams()
  const active = tab ?? 'general'
  const [mode, setMode] = useState<SettingsMode>(readMode)
  const [tool, setTool] = useState<ToolState>({ dirty: false, error: null, savedAt: null, hasAdvanced: false, saving: false })
  const actions = useRef<ToolActions>({ save: () => {}, discard: () => {} })

  useEffect(() => {
    try {
      localStorage.setItem(MODE_KEY, mode)
    } catch {
      // private mode etc. -- the toggle still works for this session
    }
  }, [mode])

  // A tab switch must not inherit the previous tab's dirty/saved state.
  useEffect(() => {
    setTool({ dirty: false, error: null, savedAt: null, hasAdvanced: false, saving: false })
  }, [active])

  if (!TAB_KEYS.includes(active)) return <Navigate to="/settings/general" replace />
  const label = SETTINGS_TABS.find((t) => t.key === active)?.label ?? 'General'

  return (
    <ToolbarCtx.Provider value={{ setTool, actions }}>
      <ModeContext.Provider value={mode}>
        <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
          <h1 style={{ margin: 0, fontSize: 17, flex: 1 }}>Settings · {label}</h1>
          <div role="group" aria-label="Simple or advanced settings" style={{ display: 'flex', border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden' }}>
            {(['simple', 'advanced'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                aria-pressed={mode === m}
                style={{
                  border: 0, padding: '5px 12px', fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
                  background: mode === m ? 'var(--accent)' : 'transparent',
                  color: mode === m ? '#082e28' : 'var(--text-dim)',
                  textTransform: 'capitalize',
                }}
              >
                {m}
              </button>
            ))}
          </div>
          <span role="status" style={{ fontSize: 13, minWidth: 130, textAlign: 'right' }}>
            {tool.dirty
              ? <span style={{ color: 'var(--yellow)' }}><span aria-hidden="true">! </span>Unsaved changes</span>
              : tool.savedAt !== null
                ? <span style={{ color: 'var(--green)' }}><span aria-hidden="true">✓ </span>Saved {formatRelative(new Date(tool.savedAt).toISOString())}</span>
                : null}
          </span>
          {tool.dirty && (
            <button className="btn" onClick={() => actions.current.discard()}>Discard</button>
          )}
          <button className="btn btn-primary" onClick={() => actions.current.save()} disabled={!tool.dirty || tool.saving}>
            {tool.saving ? <span className="spinner" /> : 'Save changes'}
          </button>
        </div>

        <div data-content style={{ padding: 20, maxWidth: 900, display: 'flex', flexDirection: 'column', gap: 16 }}>
          {tool.error && <div className="error-banner" role="alert">{tool.error}</div>}
          {active === 'general' && <GeneralTab />}
          {active === 'sync' && <SyncTab />}
          {active === 'correctness' && <CorrectnessTab />}
          {GENERATE_UI && active === 'generate' && <GenerateTab />}
          {active === 'automation' && <AutomationTab />}
          {active === 'bazarr' && <BazarrTab />}
          {active === 'scheduling' && <SchedulingTab />}
          {active === 'log' && <LogTab />}
          {active === 'account' && <AccountTab />}
          {mode === 'advanced' && !tool.hasAdvanced && (
            <div className="text-dim" style={{ fontSize: 13 }}>This tab has no advanced settings.</div>
          )}
        </div>
      </ModeContext.Provider>
    </ToolbarCtx.Provider>
  )
}
