import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type {
  AppLogLine,
  AutomationSettings,
  BazarrSettings,
  CorrectnessSettings,
  GeneralSettings,
  LibraryResponse,
  LogSettings,
  SchedulingSettings,
  SyncSettings,
} from '../api/types'
import { buildCron, parseCron, DAY_NAMES, type FriendlySchedule, type ScheduleMode } from '../lib/cron'
import { formatRelative } from '../lib/format'
import ConfirmDialog from '../components/ConfirmDialog'
import FolderBrowser from '../components/FolderBrowser'
import LanguageMultiSelect from '../components/LanguageMultiSelect'
import CopyLogButton from '../components/CopyLogButton'
import styles from './Settings.module.css'

function SaveBar({ busy, saved, error }: { busy: boolean; saved: boolean; error: string | null }) {
  return (
    <div className={styles.actions}>
      <button type="submit" className="btn btn-primary" disabled={busy}>
        {busy ? <span className="spinner" /> : 'Save'}
      </button>
      {saved && <span className={styles.savedMsg}>Saved.</span>}
      {error && <span style={{ color: 'var(--red)', fontSize: 13 }}>{error}</span>}
    </div>
  )
}

function useGroup<T>(group: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)

  function load() {
    api
      .get<T>(`/settings/${group}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [group])
  return { data, setData, error, setError }
}

function useSave(group: string) {
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save<T>(values: Record<string, unknown>, onSaved?: (r: T) => void) {
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      const r = await api.put<T>(`/settings/${group}`, { values })
      setSaved(true)
      onSaved?.(r)
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return { busy, saved, error, save }
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
    <div className={styles.timeRow}>
      <select value={hour} onChange={(e) => onChange(`${e.target.value}:${minute}`)}>
        {HOURS.map((h) => (
          <option key={h} value={h}>{h}</option>
        ))}
      </select>
      <span className={styles.timeColon}>:</span>
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
    <div className={styles.row}>
      <Field label="Host / IP">
        <input type="text" value={host} onChange={(e) => onHost(e.target.value)} placeholder="192.168.1.32" />
      </Field>
      <Field label="Port">
        <input type="text" value={port} onChange={(e) => onPort(e.target.value)} placeholder="8989" />
      </Field>
    </div>
  )
}

// Native `title` tooltips turned out not to show reliably for everyone, so this is a plain
// CSS hover/focus bubble instead — always renders the same way regardless of browser.
function Tip({ text }: { text: string }) {
  return (
    <span className={styles.tip} tabIndex={0}>
      ?<span className={styles.tipBubble}>{text}</span>
    </span>
  )
}

function Field({ label, tip, children }: { label: string; tip?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>
        {label}
        {tip && <Tip text={tip} />}
      </label>
      {children}
    </div>
  )
}

interface PathHealth {
  exists: boolean
  entry_count: number | null
}

function SingleFolderField({ label, tip, path, onChange }: { label: string; tip?: string; path: string; onChange: (path: string) => void }) {
  const [browsing, setBrowsing] = useState(false)
  const [health, setHealth] = useState<PathHealth | null>(null)

  useEffect(() => {
    if (!path) { setHealth(null); return }
    api
      .get<PathHealth>(`/browse/check?path=${encodeURIComponent(path)}`)
      .then(setHealth)
      .catch(() => setHealth({ exists: false, entry_count: null }))
  }, [path])

  return (
    <div className="field">
      <label>
        {label}
        {tip && <Tip text={tip} />}
      </label>
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
// consolidates what used to be a checkbox in each of three different tabs (Sync, LLM settings,
// and General) into a single place, since the "does this run, and when" question spans all three
// features the same way. Renders inside AutomationTab's own single form/save button (below) —
// spans three settings groups (general/sync/correctness) on top of that tab's own "automation"
// group, so the hooks live here but there is deliberately only one save button for all four.
function useWhatRuns() {
  const general = useGroup<GeneralSettings>('general')
  const sync = useGroup<SyncSettings>('sync')
  const correctness = useGroup<CorrectnessSettings>('correctness')
  const loadError = general.error || sync.error || correctness.error
  return { general, sync, correctness, loadError }
}

const AUTO_ACTION_TIP =
  'off = flag only. quarantine = move it aside. blacklist = + tell Bazarr. remediate = + fetch a replacement.'

function AutoActionSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="off">off</option>
      <option value="quarantine">quarantine</option>
      <option value="blacklist">blacklist</option>
      <option value="remediate">remediate</option>
    </select>
  )
}

function WhatRunsTable({ general, sync, correctness }: ReturnType<typeof useWhatRuns>) {
  if (!general.data || !sync.data || !correctness.data) return <span className="spinner" />
  const g = general.data, s = sync.data, c = correctness.data

  return (
    <>
      <h3 style={{ marginTop: 0, marginBottom: 4 }}>What runs</h3>
      <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 620, marginTop: 0, marginBottom: 14 }}>
        The Manual column is for a Scan/Rescan you click yourself. The next column is shared by
        the scheduled sweep and the Bazarr poll — both scan on their own without you asking, so
        they use the same switch.
      </p>
      <table className={styles.whatRunsTable}>
        <thead>
          <tr>
            <th></th>
            <th>Manual scan</th>
            <th>Scheduled sweep / Bazarr poll</th>
            <th>Action if SUSPECT</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              Sync
              <Tip text="Fixes subtitle timing to match the audio." />
            </td>
            <td>
              <input type="checkbox" checked={s.enabled} onChange={(e) => sync.setData({ ...s, enabled: e.target.checked })} />
            </td>
            <td>
              <input
                type="checkbox"
                checked={g.auto_scan_sync_enabled}
                onChange={(e) => general.setData({ ...g, auto_scan_sync_enabled: e.target.checked })}
              />
            </td>
            <td className="text-faint">—</td>
          </tr>
          <tr>
            <td>
              Correctness check
              <Tip text="Compares a bit of audio to the subtitle with Whisper, to catch a mismatched file. Needs an API key on the LLM tab." />
            </td>
            <td>
              <input type="checkbox" checked={c.enabled} onChange={(e) => correctness.setData({ ...c, enabled: e.target.checked })} />
            </td>
            <td>
              <input
                type="checkbox"
                checked={g.auto_scan_correctness_enabled}
                onChange={(e) => general.setData({ ...g, auto_scan_correctness_enabled: e.target.checked })}
              />
            </td>
            <td className={styles.actionCell}>
              <AutoActionSelect value={c.auto_action} onChange={(v) => correctness.setData({ ...c, auto_action: v as CorrectnessSettings['auto_action'] })} />
              <Tip text={AUTO_ACTION_TIP} />
            </td>
          </tr>
          <tr>
            <td>
              Line-order check
              <Tip text="Catches two-line entries in the wrong order. Auto-fixes if the correctness check is on too; otherwise just flags likely cases." />
            </td>
            <td>
              <input
                type="checkbox"
                checked={s.line_order_enabled}
                onChange={(e) => sync.setData({ ...s, line_order_enabled: e.target.checked })}
              />
            </td>
            <td>
              <input
                type="checkbox"
                checked={g.auto_scan_line_order_enabled}
                onChange={(e) => general.setData({ ...g, auto_scan_line_order_enabled: e.target.checked })}
              />
            </td>
            <td className={styles.actionCell}>
              <AutoActionSelect
                value={s.line_order_auto_action}
                onChange={(v) => sync.setData({ ...s, line_order_auto_action: v as SyncSettings['line_order_auto_action'] })}
              />
              <Tip text={`${AUTO_ACTION_TIP} Only for a widespread pattern -- a single swap is just fixed in place.`} />
            </td>
          </tr>
        </tbody>
      </table>
    </>
  )
}

function GeneralTab() {
  const { data, setData, error: loadError } = useGroup<GeneralSettings>('general')
  const { data: bazarrData } = useGroup<BazarrSettings>('bazarr')
  const { busy, saved, error, save } = useSave('general')
  const [detecting, setDetecting] = useState(false)
  const [detectProgress, setDetectProgress] = useState<{ done: number; total: number } | null>(null)
  const [detectResult, setDetectResult] = useState<string | null>(null)
  const [detectError, setDetectError] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [showBazarrWarning, setShowBazarrWarning] = useState(false)

  // Polls the actual server-side state rather than trusting only this component's own
  // `detecting` -- switching Settings tabs unmounts this component entirely, so on its own
  // that state can't survive a tab switch and back while a rescan is still running. This
  // poll (started fresh on every mount) picks the real state back up either way.
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const s = await api.get<{ running: boolean; done: number; total: number; cancelled: boolean }>(
          '/library/rescan/status',
        )
        if (cancelled) return
        setDetecting(s.running)
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
    // No `finally { setDetecting(false) }` here on purpose -- the poll loop above picks up
    // the real "running: false" from the server, so it stays correct even if this component
    // unmounted (tab switch) before this request resolved.
  }

  // Without Bazarr configured, embedded-subtitle detection has no bulk source to read from
  // (see bazarr_embedded_subtitle_langs) and falls all the way back to per-file ffprobe for
  // everything -- fine for a small library, slow for a large one. Warn instead of silently
  // running into that on a library the size of a real one.
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
      {!data ? (
        <span className="spinner" />
      ) : (
        <form
          className={`card ${styles.formCard}`}
          onSubmit={(e) => {
            e.preventDefault()
            // Only this card's own fields -- NOT auto_scan_* (Settings -> Automation's "What
            // runs" table owns those, via its own separate fetch/save of the same "general"
            // group; sending this form's possibly-stale copy of them here would silently undo a
            // change just made there).
            save({
              movies_folder: data.movies_folder,
              series_folder: data.series_folder,
              subtitle_langs: data.subtitle_langs,
              backup_originals: data.backup_originals,
            })
          }}
        >
          {loadError && <div className="error-banner">{loadError}</div>}
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
              <button type="button" className="btn btn-sm" onClick={handleDetectClick}>
                Detect now
              </button>
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
          <Field label="Subtitle languages" tip="Empty = all languages allowed.">
            <LanguageMultiSelect codes={data.subtitle_langs} onChange={(subtitle_langs) => setData({ ...data, subtitle_langs })} />
          </Field>
          <div className={styles.checkRow}>
            <input
              id="backup_originals"
              type="checkbox"
              checked={data.backup_originals}
              onChange={(e) => setData({ ...data, backup_originals: e.target.checked })}
            />
            <label htmlFor="backup_originals">
              Back up subtitles before overwriting them
              <Tip text="Off by default. When on, a copy is saved before any automatic edit overwrites a subtitle." />
            </label>
          </div>
          <SaveBar busy={busy} saved={saved} error={error} />
        </form>
      )}
    </>
  )
}

function SyncTab() {
  const { data, setData, error: loadError } = useGroup<SyncSettings>('sync')
  const { busy, saved, error, save } = useSave('sync')
  if (!data) return <span className="spinner" />

  return (
    <form
      className={`card ${styles.formCard}`}
      onSubmit={(e) => {
        e.preventDefault()
        // Everything except enabled/line_order_enabled/line_order_auto_action -- those three are
        // owned by Settings -> Automation's "What runs" table (its own separate fetch/save of
        // this same group); sending this form's possibly-stale copy of them would silently undo
        // a change just made there.
        const { enabled: _enabled, line_order_enabled: _lineOrderEnabled,
          line_order_auto_action: _lineOrderAutoAction, ...rest } = data
        save(rest as unknown as Record<string, unknown>)
      }}
    >
      {loadError && <div className="error-banner">{loadError}</div>}
      <Field label="Split penalty" tip="Usually somewhere in 1-20. Lower values split the sync into more, smaller segments.">
        <input
          type="number"
          value={data.split_penalty}
          onChange={(e) => setData({ ...data, split_penalty: Number(e.target.value) })}
        />
      </Field>
      <Field label="Min. change (seconds)" tip="Ignore corrections smaller than this.">
        <input
          type="number"
          step="0.05"
          value={data.min_change_seconds}
          onChange={(e) => setData({ ...data, min_change_seconds: Number(e.target.value) })}
        />
      </Field>
      <Field label="Whisper samples per file" tip="How many audio clips to sample per file when checking correctness. Spread evenly, each picked from nearby dialogue.">
        <input
          type="number"
          value={data.sample_count}
          onChange={(e) => setData({ ...data, sample_count: Number(e.target.value) })}
        />
      </Field>
      <Field label="Clip length (s)" tip="Length of each sampled audio clip sent for transcription.">
        <input
          type="number"
          value={data.clip_seconds}
          onChange={(e) => setData({ ...data, clip_seconds: Number(e.target.value) })}
        />
      </Field>
      <Field label="Comparison window (min)" tip="How much subtitle text around each sample point to compare the transcript against.">
        <input
          type="number"
          step="0.1"
          value={data.window_minutes}
          onChange={(e) => setData({ ...data, window_minutes: Number(e.target.value) })}
        />
      </Field>
      <Field label="Overlap threshold" tip="Minimum word-overlap for a sample to count as a match. 0.25 works well in practice.">
        <input
          type="number"
          step="0.01"
          value={data.overlap_threshold}
          onChange={(e) => setData({ ...data, overlap_threshold: Number(e.target.value) })}
        />
      </Field>

      <h3 style={{ marginBottom: 4 }}>Line-order check</h3>
      <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 480, marginTop: 0, marginBottom: 14 }}>
        Catches two-line subtitle entries where the lines are in the wrong order (line 2 spoken
        before line 1). Turn it on or off from Settings → Automation → What runs — these fields
        just fine-tune it once it's on.
      </p>
      <div className={styles.checkRow}>
        <input
          id="line_order_audio_confirm"
          type="checkbox"
          checked={data.line_order_audio_confirm}
          onChange={(e) => setData({ ...data, line_order_audio_confirm: e.target.checked })}
        />
        <label htmlFor="line_order_audio_confirm">
          Act on the audio check
          <Tip text="The audio check always runs -- this just decides whether to act on it. On: confirmed swaps get auto-fixed. Off: only flagged for you to check." />
        </label>
      </div>
      <div className={styles.row}>
        <Field
          label="Widespread-swap threshold (%)"
          tip="Share of tested candidates that must be confirmed swapped (by Whisper AND an LLM check) before the file is treated as SUSPECT."
        >
          <input
            type="number" step="1" min="1" max="100"
            value={Math.round(data.line_order_swap_threshold_pct * 100)}
            onChange={(e) => setData({ ...data, line_order_swap_threshold_pct: Number(e.target.value) / 100 })}
          />
        </Field>
        <Field
          label="Widespread-swap minimum count"
          tip="Minimum confirmed count needed too, so a tiny sample can't trigger this on its own."
        >
          <input
            type="number" step="1" min="1" max="100"
            value={data.line_order_swap_threshold_min}
            onChange={(e) => setData({ ...data, line_order_swap_threshold_min: Number(e.target.value) })}
          />
        </Field>
      </div>
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

function CorrectnessTab() {
  const { data, setData, error: loadError } = useGroup<CorrectnessSettings>('correctness')
  const { busy, saved, error, save } = useSave('correctness')
  const [newGroqKey, setNewGroqKey] = useState('')
  const [newOpenRouterKey, setNewOpenRouterKey] = useState('')
  if (!data) return <span className="spinner" />

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!data) return
    // enabled/auto_action are excluded -- owned by Settings -> Automation's "What runs" table
    // (its own separate fetch/save of this same group); sending this form's possibly-stale copy
    // would silently undo a change just made there.
    const { groq_api_key: _omit1, openrouter_api_key: _omit2, enabled: _omit3, auto_action: _omit4, ...rest } = data
    const values: Record<string, unknown> = { ...rest }
    if (newGroqKey) values.groq_api_key = newGroqKey
    if (newOpenRouterKey) values.openrouter_api_key = newOpenRouterKey
    save(values)
  }

  const isOpenRouter = data.stt_provider === 'openrouter'

  return (
    <form className={`card ${styles.formCard}`} onSubmit={onSubmit}>
      {loadError && <div className="error-banner">{loadError}</div>}
      <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 480, marginTop: 0, marginBottom: 14 }}>
        Turned on/off from Settings → Automation → What runs — the fields below configure the
        provider it uses once it's on.
      </p>
      <Field label="Provider" tip="Used for both transcription and translation.">
        <select value={data.stt_provider} onChange={(e) => setData({ ...data, stt_provider: e.target.value as CorrectnessSettings['stt_provider'] })}>
          <option value="groq">Groq</option>
          <option value="openrouter">OpenRouter</option>
        </select>
      </Field>

      {!isOpenRouter && (
        <>
          <Field
            label="Groq API key"
            tip={data.groq_api_key.is_set ? 'A key is already saved — type here only to replace it.' : 'Not set yet.'}
          >
            <input
              type="text"
              autoComplete="off"
              placeholder={data.groq_api_key.is_set ? '••••••••••••••••  (saved — leave blank to keep)' : 'gsk_…'}
              value={newGroqKey}
              onChange={(e) => setNewGroqKey(e.target.value)}
            />
          </Field>
          <div className={styles.row}>
            <Field label="Whisper model">
              <input type="text" value={data.groq_model} onChange={(e) => setData({ ...data, groq_model: e.target.value })} />
            </Field>
            <Field
              label="Whisper fallback model"
              tip="Used only if the main model hits its rate limit. Pick a different model for it to actually help."
            >
              <input
                type="text"
                value={data.groq_model_fallback}
                onChange={(e) => setData({ ...data, groq_model_fallback: e.target.value })}
              />
            </Field>
          </div>
          <div className={styles.row}>
            <Field label="Translation model (LLM)">
              <input type="text" value={data.groq_llm_model} onChange={(e) => setData({ ...data, groq_llm_model: e.target.value })} />
            </Field>
          </div>
          <Field label="Translation fallback model">
            <input
              type="text"
              value={data.groq_llm_model_fallback}
              onChange={(e) => setData({ ...data, groq_llm_model_fallback: e.target.value })}
            />
          </Field>
        </>
      )}

      {isOpenRouter && (
        <>
          <Field
            label="OpenRouter API key"
            tip={data.openrouter_api_key.is_set ? 'A key is already saved — type here only to replace it.' : 'Not set yet.'}
          >
            <input
              type="text"
              autoComplete="off"
              placeholder={data.openrouter_api_key.is_set ? '••••••••••••••••  (saved — leave blank to keep)' : 'sk-or-…'}
              value={newOpenRouterKey}
              onChange={(e) => setNewOpenRouterKey(e.target.value)}
            />
          </Field>
          <div className={styles.row}>
            <Field label="Whisper model">
              <input
                type="text"
                value={data.openrouter_stt_model}
                onChange={(e) => setData({ ...data, openrouter_stt_model: e.target.value })}
              />
            </Field>
            <Field label="Whisper fallback model">
              <input
                type="text"
                value={data.openrouter_stt_model_fallback}
                onChange={(e) => setData({ ...data, openrouter_stt_model_fallback: e.target.value })}
              />
            </Field>
          </div>
          <div className={styles.row}>
            <Field label="Translation model (LLM)">
              <input
                type="text"
                value={data.openrouter_llm_model}
                onChange={(e) => setData({ ...data, openrouter_llm_model: e.target.value })}
              />
            </Field>
            <Field label="Translation fallback model">
              <input
                type="text"
                value={data.openrouter_llm_model_fallback}
                onChange={(e) => setData({ ...data, openrouter_llm_model_fallback: e.target.value })}
              />
            </Field>
          </div>
        </>
      )}

      <Field label="Required audio language" tip="Empty = run regardless of spoken language.">
        <input
          type="text"
          value={data.require_audio_lang}
          onChange={(e) => setData({ ...data, require_audio_lang: e.target.value })}
        />
      </Field>
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

function AutomationTab() {
  const whatRuns = useWhatRuns()
  const { data, setData, error: automationLoadError } = useGroup<AutomationSettings>('automation')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { general, sync, correctness } = whatRuns
  if (!general.data || !sync.data || !correctness.data || !data) return <span className="spinner" />
  const g = general.data, s = sync.data, c = correctness.data

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setSaved(false)
    setError(null)
    try {
      await Promise.all([
        api.put('/settings/general', {
          values: {
            auto_scan_sync_enabled: g.auto_scan_sync_enabled,
            auto_scan_correctness_enabled: g.auto_scan_correctness_enabled,
            auto_scan_line_order_enabled: g.auto_scan_line_order_enabled,
          },
        }),
        api.put('/settings/sync', {
          values: { enabled: s.enabled, line_order_enabled: s.line_order_enabled, line_order_auto_action: s.line_order_auto_action },
        }),
        api.put('/settings/correctness', { values: { enabled: c.enabled, auto_action: c.auto_action } }),
        api.put('/settings/automation', { values: data }),
      ])
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className={`card ${styles.formCard}`} onSubmit={onSubmit}>
      {(whatRuns.loadError || automationLoadError) && (
        <div className="error-banner">{whatRuns.loadError || automationLoadError}</div>
      )}
      <WhatRunsTable {...whatRuns} />
      <Field label="Max. remediation attempts">
        <input
          type="number"
          value={data.remediate_max_attempts}
          onChange={(e) => setData({ ...data, remediate_max_attempts: Number(e.target.value) })}
        />
      </Field>
      <Field
        label="Minimum Bazarr score to try (remediation, %)"
        tip="Bazarr's own match score for a candidate -- one below this is skipped during remediation. 0 = try everything."
      >
        <input
          type="number"
          step="1"
          min="0"
          max="100"
          value={data.remediate_min_score}
          onChange={(e) => setData({ ...data, remediate_min_score: Number(e.target.value) })}
        />
      </Field>
      <div className={styles.checkRow}>
        <input
          id="dry_run"
          type="checkbox"
          checked={data.dry_run}
          onChange={(e) => setData({ ...data, dry_run: e.target.checked })}
        />
        <label htmlFor="dry_run">Dry run — show what would happen, don't change anything</label>
      </div>
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

function BazarrTab() {
  const { data, setData, error: loadError } = useGroup<BazarrSettings>('bazarr')
  const { busy, saved, error, save } = useSave('bazarr')
  const [newApiKey, setNewApiKey] = useState('')
  const [testResult, setTestResult] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [host, setHost] = useState('')
  const [port, setPort] = useState('')
  const initialized = useRef(false)

  useEffect(() => {
    if (data && !initialized.current) {
      const hp = urlToHostPort(data.url)
      setHost(hp.host)
      setPort(hp.port)
      initialized.current = true
    }
  }, [data])

  if (!data) return <span className="spinner" />

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

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!data) return
    const { api_key: _omit, ...rest } = data
    const values: Record<string, unknown> = { ...rest, url: hostPortToUrl(host, port) }
    if (newApiKey) values.api_key = newApiKey
    save(values)
  }

  async function testConnection() {
    setTesting(true)
    setTestResult(null)
    try {
      // Tests what's in the form RIGHT NOW, without requiring Save first — url is always
      // sent (so a field change is tested immediately); api_key only if the user typed a
      // new one (otherwise the already-saved one is used, see backend).
      const r = await api.post<{ ok: boolean; bazarr_version: string | null }>('/settings/bazarr/test-connection', {
        url: hostPortToUrl(host, port),
        api_key: newApiKey || undefined,
      })
      setTestResult(`Connected — Bazarr version ${r.bazarr_version ?? 'unknown'}`)
    } catch (err) {
      setTestResult(err instanceof ApiError ? err.message : String(err))
    } finally {
      setTesting(false)
    }
  }

  return (
    <form className={`card ${styles.formCard}`} onSubmit={onSubmit}>
      {loadError && <div className="error-banner">{loadError}</div>}
      <p className="text-dim" style={{ fontSize: 12.5, maxWidth: 480, marginTop: 0, marginBottom: 16 }}>
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
        />
      </Field>
      <div className={styles.actions} style={{ marginTop: 0, marginBottom: 14 }}>
        <button type="button" className="btn" disabled={testing} onClick={testConnection}>
          {testing ? <span className="spinner" /> : 'Test connection'}
        </button>
        {testResult && <span style={{ fontSize: 13 }}>{testResult}</span>}
      </div>
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

function SchedulingTab() {
  const { data, setData, error: loadError } = useGroup<SchedulingSettings>('scheduling')
  const { busy, saved, error, save } = useSave('scheduling')
  const [schedule, setSchedule] = useState<FriendlySchedule | null>(null)
  const initialized = useRef(false)

  useEffect(() => {
    if (data && !initialized.current) {
      setSchedule(parseCron(data.cron))
      initialized.current = true
    }
  }, [data])

  if (!data || !schedule) return <span className="spinner" />

  function updateSchedule(patch: Partial<FriendlySchedule>) {
    const next = { ...schedule!, ...patch }
    setSchedule(next)
    if (next.mode !== 'advanced') {
      setData({ ...data!, cron: buildCron({ mode: next.mode, time: next.time, dayOfWeek: next.dayOfWeek }) })
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    save(data as unknown as Record<string, unknown>)
  }

  return (
    <form className={`card ${styles.formCard}`} onSubmit={onSubmit}>
      {loadError && <div className="error-banner">{loadError}</div>}
      <Field label="Run a sweep">
        <select
          value={schedule.mode}
          onChange={(e) => updateSchedule({ mode: e.target.value as ScheduleMode })}
        >
          <option value="daily">Every day</option>
          <option value="weekly">Every week</option>
          <option value="advanced">Advanced (raw cron)</option>
        </select>
      </Field>

      {schedule.mode !== 'advanced' && (
        <div className={styles.row}>
          {schedule.mode === 'weekly' && (
            <Field label="On">
              <select
                value={schedule.dayOfWeek}
                onChange={(e) => updateSchedule({ dayOfWeek: Number(e.target.value) })}
              >
                {DAY_NAMES.map((name, i) => (
                  <option key={name} value={i}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label="At (UTC)" tip="The container's own clock/timezone doesn't matter here — this always runs in UTC.">
            <TimeOfDayField value={schedule.time} onChange={(time) => updateSchedule({ time })} />
          </Field>
        </div>
      )}

      {schedule.mode === 'advanced' && (
        <Field label="Cron expression (UTC)" tip="Standard 5-field cron, e.g. '0 4 * * 0' = Sunday at 04:00 UTC.">
          <input type="text" value={data.cron} onChange={(e) => setData({ ...data, cron: e.target.value })} />
        </Field>
      )}

      <div className={styles.checkRow}>
        <input
          id="run_on_start"
          type="checkbox"
          checked={data.run_on_start}
          onChange={(e) => setData({ ...data, run_on_start: e.target.checked })}
        />
        <label htmlFor="run_on_start">Also run a sweep immediately when the container starts</label>
      </div>
      <div className={styles.checkRow}>
        <input
          id="poll_new_media_enabled"
          type="checkbox"
          checked={data.poll_new_media_enabled}
          onChange={(e) => setData({ ...data, poll_new_media_enabled: e.target.checked })}
        />
        <label htmlFor="poll_new_media_enabled">
          Scan when Bazarr has a subtitle ready
          <Tip text="Scans an item as soon as Bazarr's satisfied it (needs a URL + API key on Settings → Bazarr). What the scan does is set under Automation → What runs." />
        </label>
      </div>
      {data.poll_new_media_enabled && (
        <Field label="Check every (minutes)" tip="How often to poll Bazarr's wanted-subtitles lists.">
          <input
            type="number"
            min="1"
            value={data.poll_new_media_interval_minutes}
            onChange={(e) => setData({ ...data, poll_new_media_interval_minutes: Number(e.target.value) })}
          />
        </Field>
      )}
      <div className={styles.checkRow}>
        <input
          id="poll_library_enabled"
          type="checkbox"
          checked={data.poll_library_enabled}
          onChange={(e) => setData({ ...data, poll_library_enabled: e.target.checked })}
        />
        <label htmlFor="poll_library_enabled">
          Watch media folders for new files
          <Tip text="Rechecks the folders for new files and refreshes the Library page. Discovery only -- no sync, no correctness check." />
        </label>
      </div>
      {data.poll_library_enabled && (
        <Field label="Check every (hours)" tip="How often to re-walk the media folders for new/removed files. Stored as minutes under the hood -- fractional hours (e.g. 0.5) are fine.">
          <input
            type="number"
            min="0.1"
            step="0.5"
            value={data.poll_library_interval_minutes / 60}
            onChange={(e) => setData({ ...data, poll_library_interval_minutes: Math.round(Number(e.target.value) * 60) })}
          />
        </Field>
      )}
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

const LOG_POLL_MS = 3000
const LOG_MAX_LINES = 2000

function LogTab() {
  const { data, setData, error: loadError } = useGroup<LogSettings>('log')
  const { busy, saved, error, save } = useSave('log')
  const [lines, setLines] = useState<AppLogLine[]>([])
  const [viewerError, setViewerError] = useState<string | null>(null)
  const logBoxRef = useRef<HTMLDivElement>(null)
  const lastIdRef = useRef(0)

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

  useEffect(() => {
    if (logBoxRef.current) logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight
  }, [lines])

  return (
    <>
      {!data ? (
        <span className="spinner" />
      ) : (
        <form
          className={`card ${styles.formCard}`}
          onSubmit={(e) => {
            e.preventDefault()
            save(data as unknown as Record<string, unknown>)
          }}
        >
          {loadError && <div className="error-banner">{loadError}</div>}
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
          <SaveBar busy={busy} saved={saved} error={error} />
        </form>
      )}

      <div className={`card ${styles.formCard}`}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h3 style={{ marginTop: 0, marginBottom: 4 }}>Recent log</h3>
            <p className="text-dim" style={{ fontSize: 12.5, marginTop: 0, marginBottom: 12 }}>
              Updates on its own every few seconds. This is the whole app's log, not just one run —
              check here if something looks off and Activity doesn't explain why.
            </p>
          </div>
          <CopyLogButton lines={lines} />
        </div>
        {viewerError && <div className="error-banner">{viewerError}</div>}
        <div className={styles.logBox} ref={logBoxRef}>
          {lines.length === 0 && <div className="text-faint">No log lines yet.</div>}
          {lines.map((l) => (
            <div key={l.id} className={`${styles.logLine} ${styles[`level${l.level}`] ?? ''}`}>
              <span className={styles.ts}>{new Date(l.ts).toLocaleTimeString('en-US')}</span>
              {l.message}
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

function AccountTab() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (next.length < 5) {
      setError('New password must be at least 5 characters')
      return
    }
    if (next !== confirm) {
      setError('Passwords do not match')
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/change-password', { current_password: current, new_password: next })
      setSaved(true)
      setCurrent('')
      setNext('')
      setConfirm('')
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className={`card ${styles.formCard}`} onSubmit={onSubmit}>
      <Field label="Current password">
        <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field label="New password">
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      <Field label="Repeat new password">
        <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </Field>
      <SaveBar busy={busy} saved={saved} error={error} />
    </form>
  )
}

export default function Settings() {
  const { tab } = useParams()
  const active = tab ?? 'general'

  return (
    <div>
      <h1>Settings</h1>
      {active === 'general' && <GeneralTab />}
      {active === 'sync' && <SyncTab />}
      {active === 'correctness' && <CorrectnessTab />}
      {active === 'automation' && <AutomationTab />}
      {active === 'bazarr' && <BazarrTab />}
      {active === 'scheduling' && <SchedulingTab />}
      {active === 'log' && <LogTab />}
      {active === 'account' && <AccountTab />}
    </div>
  )
}
