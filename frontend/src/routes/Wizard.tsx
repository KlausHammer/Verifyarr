import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { BazarrSettings, CorrectnessSettings, GeneralSettings, NextRunResponse, SchedulingSettings } from '../api/types'
import FolderBrowser from '../components/FolderBrowser'
import LanguageMultiSelect from '../components/LanguageMultiSelect'
import { useToasts } from '../hooks/useToasts'
import { buildCron, DAY_NAMES, parseCron } from '../lib/cron'

const STEPS = ['Media folders', 'Languages', 'Bazarr', 'Speech recognition', 'Bad subtitles', 'Schedule']

interface WizVals {
  moviesDir: string
  seriesDir: string
  langs: string[]
  bazarrUrl: string
  bazarrKey: string
  bazarrKeySet: boolean
  mapFrom: string
  mapTo: string
  speech: 'local' | 'cloud'
  cloud: 'groq' | 'openrouter'
  apiKey: string
  cloudKeySet: boolean
  badAction: 'off' | 'quarantine' | 'blacklist' | 'remediate'
  schedMode: 'nightly' | 'weekly' | 'keep'
  schedTime: string
  schedDay: number
  keepCron: string
}

type Check = { status: 'checking' } | { status: 'ok' | 'bad' | 'warn'; msg: string } | null
type BzTest = { status: 'idle' | 'testing' | 'ok' | 'fail'; msg: string }

function RadioCards<T extends string>({ name, options, value, onPick }: {
  name: string
  options: { value: T; title: string; desc: string }[]
  value: T
  onPick: (v: T) => void
}) {
  return (
    <div role="radiogroup" aria-label={name} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {options.map((o) => {
        const sel = value === o.value
        return (
          <label
            key={o.value}
            style={{
              display: 'flex', gap: 10, alignItems: 'flex-start', padding: '11px 12px',
              border: `1px solid ${sel ? 'var(--accent-dim)' : 'var(--border)'}`,
              background: sel ? 'rgba(94,234,212,.06)' : 'transparent',
              borderRadius: 'var(--radius)', cursor: 'pointer', margin: 0, color: 'var(--text)',
              fontSize: 14, fontWeight: 400,
            }}
          >
            <input type="radio" name={name} checked={sel} onChange={() => onPick(o.value)} style={{ marginTop: 3, accentColor: 'var(--accent)' }} />
            <span>
              <span style={{ fontWeight: 600, display: 'block' }}>{o.title}</span>
              <span className="text-dim" style={{ fontSize: 13, lineHeight: 1.45 }}>{o.desc}</span>
            </span>
          </label>
        )
      })}
    </div>
  )
}

export default function Wizard() {
  const [params] = useSearchParams()
  const fromSettings = params.get('from') === 'settings'
  const [vals, setVals] = useState<WizVals | null>(null)
  const [failed, setFailed] = useState(false)
  const [step, setStep] = useState(0)
  const [checks, setChecks] = useState<{ moviesDir: Check; seriesDir: Check }>({ moviesDir: null, seriesDir: null })
  const [browsing, setBrowsing] = useState<'moviesDir' | 'seriesDir' | null>(null)
  const [mapTouched, setMapTouched] = useState(false)
  const [bz, setBz] = useState<BzTest>({ status: 'idle', msg: '' })
  const [finishing, setFinishing] = useState(false)
  const [finishError, setFinishError] = useState<string | null>(null)
  const [serverTz, setServerTz] = useState<string | null>(null)
  const timers = useRef<{ moviesDir?: ReturnType<typeof setTimeout>; seriesDir?: ReturnType<typeof setTimeout> }>({})
  const { toast } = useToasts()
  const navigate = useNavigate()

  useEffect(() => {
    let cancelled = false
    async function load() {
      setFailed(false)
      try {
        const [g, b, c, s] = await Promise.all([
          api.get<GeneralSettings>('/settings/general'),
          api.get<BazarrSettings>('/settings/bazarr'),
          api.get<CorrectnessSettings>('/settings/correctness'),
          api.get<SchedulingSettings>('/settings/scheduling'),
        ])
        if (cancelled) return
        api.get<NextRunResponse>('/runs/next').then(
          (r) => { if (!cancelled) setServerTz(r.timezone ?? null) },
          () => {},
        )
        const sched = parseCron(s.cron)
        // path_map pairs are [verifyarr-path, bazarr-path].
        const firstPair = b.path_map[0]
        const cloud = c.stt_provider === 'openrouter' ? 'openrouter' : 'groq'
        setVals({
          moviesDir: fromSettings ? g.movies_folder : '',
          seriesDir: fromSettings ? g.series_folder : '',
          langs: fromSettings ? g.subtitle_langs : ['en'],
          bazarrUrl: fromSettings ? b.url : '',
          bazarrKey: '',
          bazarrKeySet: b.api_key.is_set,
          mapFrom: fromSettings && firstPair ? (firstPair[1] ?? '') : '',
          mapTo: fromSettings && firstPair ? (firstPair[0] ?? '') : '',
          speech: c.use_local_whisper ? 'local' : 'cloud',
          cloud,
          apiKey: '',
          cloudKeySet: cloud === 'groq' ? c.groq_api_key.is_set : c.openrouter_api_key.is_set,
          badAction: c.auto_action,
          schedMode: sched.mode === 'advanced' ? 'keep' : sched.mode === 'weekly' ? 'weekly' : 'nightly',
          schedTime: sched.time,
          schedDay: sched.dayOfWeek,
          keepCron: s.cron,
        })
        if (fromSettings) {
          if (g.movies_folder) check('moviesDir', g.movies_folder)
          if (g.series_folder) check('seriesDir', g.series_folder)
        }
      } catch {
        if (!cancelled) setFailed(true)
      }
    }
    load()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function setV(patch: Partial<WizVals>) {
    setVals((v) => (v ? { ...v, ...patch } : v))
  }

  function check(which: 'moviesDir' | 'seriesDir', path: string) {
    clearTimeout(timers.current[which])
    if (!path.trim()) {
      setChecks((c) => ({ ...c, [which]: null }))
      return
    }
    setChecks((c) => ({ ...c, [which]: { status: 'checking' } }))
    timers.current[which] = setTimeout(async () => {
      try {
        const r = await api.get<{ exists: boolean; entry_count: number | null }>(
          `/browse/check?path=${encodeURIComponent(path.trim())}`,
        )
        setChecks((c) => ({
          ...c,
          [which]: !r.exists
            ? { status: 'bad', msg: "Folder not found. Check the spelling, or that it's mounted into the container." }
            : r.entry_count === 0
              ? { status: 'warn', msg: 'Folder exists but is empty. Is this the right folder?' }
              : { status: 'ok', msg: `Found · ${r.entry_count} top-level item${r.entry_count === 1 ? '' : 's'}` },
        }))
      } catch {
        setChecks((c) => ({ ...c, [which]: { status: 'bad', msg: 'Could not check this folder.' } }))
      }
    }, 500)
  }

  async function testBz() {
    if (!vals) return
    setBz({ status: 'testing', msg: '' })
    try {
      const r = await api.post<{ ok: boolean; bazarr_version: string | null }>('/settings/bazarr/test-connection', {
        url: vals.bazarrUrl || undefined,
        api_key: vals.bazarrKey || undefined,
      })
      setBz({ status: 'ok', msg: `Connected to Bazarr ${r.bazarr_version ?? 'unknown'}` })
    } catch (err) {
      setBz({ status: 'fail', msg: err instanceof ApiError ? err.message : String(err) })
    }
  }

  async function finish() {
    if (!vals) return
    setFinishing(true)
    setFinishError(null)
    try {
      const correctnessValues: Record<string, unknown> = {
        use_local_whisper: vals.speech === 'local',
        stt_provider: vals.cloud,
        auto_action: vals.badAction,
      }
      if (vals.speech === 'cloud' && vals.apiKey) {
        correctnessValues[vals.cloud === 'groq' ? 'groq_api_key' : 'openrouter_api_key'] = vals.apiKey
      }
      const bazarrValues: Record<string, unknown> = { url: vals.bazarrUrl }
      if (vals.bazarrKey) bazarrValues.api_key = vals.bazarrKey
      // An untouched mapping is left alone (it may hold pairs this step can't show);
      // a touched one replaces it, and needs both halves to mean anything.
      if (mapTouched) {
        bazarrValues.path_map = vals.mapFrom.trim() && vals.mapTo.trim() ? [[vals.mapTo.trim(), vals.mapFrom.trim()]] : []
      }
      const puts = [
        api.put('/settings/general', { values: { movies_folder: vals.moviesDir, series_folder: vals.seriesDir, subtitle_langs: vals.langs } }),
        api.put('/settings/bazarr', { values: bazarrValues }),
        api.put('/settings/correctness', { values: correctnessValues }),
      ]
      if (vals.schedMode !== 'keep') {
        puts.push(api.put('/settings/scheduling', {
          values: {
            cron: vals.schedMode === 'nightly'
              ? buildCron({ mode: 'daily', time: vals.schedTime, dayOfWeek: 0 })
              : buildCron({ mode: 'weekly', time: vals.schedTime, dayOfWeek: vals.schedDay }),
          },
        }))
      }
      await Promise.all(puts)
      if (fromSettings) {
        toast('Setup saved.')
        navigate('/settings/general')
      } else {
        const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep' })
        toast('First scan started. The dashboard fills in when it finishes.', {
          kind: 'info',
          action: () => navigate(`/activity/${r.run_id}`),
          actionLabel: 'View job',
        })
        navigate('/')
      }
    } catch (err) {
      setFinishError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setFinishing(false)
    }
  }

  function go(n: number) {
    setStep(Math.max(0, Math.min(5, n)))
    setBrowsing(null)
  }

  if (failed || !vals) {
    return (
      <div style={{ minHeight: '100vh', background: 'var(--bg)', color: 'var(--text)', fontSize: 14, display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '48px 16px 32px', boxSizing: 'border-box' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 22 }}>
          <span aria-hidden="true" style={{ width: 15, height: 15, background: 'var(--accent)', transform: 'rotate(45deg)', borderRadius: 2 }} />
          <span style={{ fontWeight: 700, fontSize: 20 }}>Verifyarr</span>
        </div>
        {failed ? (
          <div className="card" style={{ width: 480, maxWidth: '100%' }}>
            <div className="error-banner" role="alert">Couldn&apos;t load the current settings.</div>
            <button className="btn" onClick={() => navigate(0)}>Try again</button>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-dim)' }}>
            <span className="spinner" />Loading…
          </div>
        )}
      </div>
    )
  }

  const v = vals
  const needsBazarr = (v.badAction === 'blacklist' || v.badAction === 'remediate') && !v.bazarrKey && !v.bazarrKeySet
  const folders: { key: 'moviesDir' | 'seriesDir'; label: string; ph: string }[] = [
    { key: 'moviesDir', label: 'Movies folder', ph: '/media/movies' },
    { key: 'seriesDir', label: 'Series folder', ph: '/media/tv' },
  ]

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)', color: 'var(--text)', fontSize: 14, display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '48px 16px 32px', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 22 }}>
        <span aria-hidden="true" style={{ width: 15, height: 15, background: 'var(--accent)', transform: 'rotate(45deg)', borderRadius: 2 }} />
        <span style={{ fontWeight: 700, fontSize: 20 }}>Verifyarr</span>
      </div>
      {browsing && (
        <FolderBrowser
          initialPath={v[browsing] || '/media'}
          onSelect={(p) => {
            setV({ [browsing]: p } as Partial<WizVals>)
            check(browsing, p)
            setBrowsing(null)
          }}
          onClose={() => setBrowsing(null)}
        />
      )}
      <div className="card" style={{ width: 760, maxWidth: '100%', padding: 0, overflow: 'hidden' }}>
        <div style={{ padding: '18px 22px 14px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
            <h1 style={{ fontSize: 19, margin: 0 }}>{fromSettings ? 'Setup wizard' : 'Set up Verifyarr'}</h1>
            <span className="text-dim" style={{ fontSize: 12.5 }}>Step {step + 1} of 6 · every step can be skipped and changed later in Settings</span>
          </div>
          <ol aria-label="Setup steps" style={{ listStyle: 'none', margin: '14px 0 0', padding: 0, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {STEPS.map((label, i) => {
              const cur = i === step
              const done = i < step
              return (
                <li key={label} style={{ flex: 1, minWidth: 92 }}>
                  <button
                    onClick={() => go(i)}
                    aria-current={cur ? 'step' : undefined}
                    style={{
                      width: '100%', background: 'none', border: 0,
                      borderTop: `3px solid ${cur ? 'var(--accent)' : done ? 'var(--accent-dim)' : 'var(--border)'}`,
                      padding: '7px 2px 0', textAlign: 'left', display: 'flex', gap: 6, alignItems: 'center',
                      color: cur ? 'var(--text)' : 'var(--text-dim)', fontSize: 12.5, fontWeight: cur ? 600 : 400, cursor: 'pointer',
                    }}
                  >
                    <span aria-hidden="true" style={{ width: 18, height: 18, borderRadius: '50%', background: cur ? 'var(--accent)' : done ? 'var(--accent-dim)' : 'var(--bg-hover)', color: cur || done ? '#082e28' : 'var(--text-dim)', fontSize: 11, fontWeight: 700, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flex: 'none' }}>
                      {done ? '✓' : i + 1}
                    </span>
                    {label}
                    <span style={{ position: 'absolute', left: -9999 }}>{done ? ' (done)' : ''}</span>
                  </button>
                </li>
              )
            })}
          </ol>
        </div>

        <div style={{ padding: '20px 22px', minHeight: 330 }}>
          {finishError && <div className="error-banner" role="alert">{finishError}</div>}

          {step === 0 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                Where are your movies and series? Use the folders as Verifyarr sees them (inside Docker this is the container path).
              </p>
              {folders.map((fd) => {
                const c = checks[fd.key]
                return (
                  <div key={fd.key} className="field" style={{ maxWidth: 'none', marginBottom: 18 }}>
                    <label htmlFor={`fd-${fd.key}`}>{fd.label}</label>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <input
                        id={`fd-${fd.key}`}
                        type="text"
                        className="mono"
                        placeholder={fd.ph}
                        value={v[fd.key]}
                        onChange={(e) => {
                          setV({ [fd.key]: e.target.value } as Partial<WizVals>)
                          check(fd.key, e.target.value)
                        }}
                        style={{ fontFamily: 'var(--mono)', fontSize: 13 }}
                      />
                      <button className="btn" type="button" onClick={() => setBrowsing(fd.key)}>Browse…</button>
                    </div>
                    <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 6, fontSize: 12.5, minHeight: 18 }}>
                      {c?.status === 'checking' && <><span className="spinner" style={{ width: 11, height: 11 }} /><span className="text-dim">Checking folder…</span></>}
                      {c?.status === 'ok' && <span style={{ color: 'var(--green)' }}>✓ {c.msg}</span>}
                      {c?.status === 'bad' && <span style={{ color: 'var(--red)' }}>✕ {c.msg}</span>}
                      {c?.status === 'warn' && <span style={{ color: 'var(--yellow)' }}>! {c.msg}</span>}
                      {!c && <span className="text-dim">{v[fd.key] ? '' : 'Not set yet'}</span>}
                    </div>
                  </div>
                )
              })}
            </>
          )}

          {step === 1 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                Which subtitle languages should Verifyarr check? Subtitles in other languages are left alone.
              </p>
              <div className="field">
                <label>Languages to check</label>
                <LanguageMultiSelect codes={v.langs} onChange={(langs) => setV({ langs })} />
                <div className="field-hint" style={{ color: 'var(--text-dim)' }}>Leave empty to check every language.</div>
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                Connecting Bazarr lets Verifyarr check new subtitles as they arrive, blacklist bad ones and fetch replacements. Without it, Verifyarr can still check and fix timing.
              </p>
              <div className="field">
                <label htmlFor="bz-u">Bazarr URL</label>
                <input id="bz-u" type="text" placeholder="http://192.168.1.20:6767" value={v.bazarrUrl} onChange={(e) => { setV({ bazarrUrl: e.target.value }); setBz({ status: 'idle', msg: '' }) }} style={{ fontFamily: 'var(--mono)', fontSize: 13 }} />
              </div>
              <div className="field">
                <label htmlFor="bz-k">API key</label>
                <input
                  id="bz-k"
                  type="password"
                  placeholder={v.bazarrKeySet ? '••••••••••••••••  (saved — leave blank to keep)' : ''}
                  value={v.bazarrKey}
                  onChange={(e) => { setV({ bazarrKey: e.target.value }); setBz({ status: 'idle', msg: '' }) }}
                />
                <div className="field-hint" style={{ color: 'var(--text-dim)' }}>In Bazarr: Settings → General → Security → API key.</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 18 }}>
                <button className="btn" type="button" onClick={testBz} disabled={bz.status === 'testing'}>
                  {bz.status === 'testing' && <span className="spinner" style={{ width: 11, height: 11 }} />}
                  Test connection
                </button>
                <span role="status" style={{ fontSize: 13, display: 'flex', gap: 6, alignItems: 'center' }}>
                  {bz.status === 'ok' && <span style={{ color: 'var(--green)' }}>✓ {bz.msg}</span>}
                  {bz.status === 'fail' && <span style={{ color: 'var(--red)' }}>✕ {bz.msg}</span>}
                  {bz.status === 'testing' && <span className="text-dim">Connecting…</span>}
                  {bz.status === 'idle' && <span className="text-dim">{v.bazarrUrl ? '' : 'Not tested yet'}</span>}
                </span>
              </div>
              <div style={{ borderTop: '1px solid var(--border)', paddingTop: 14 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>Path mapping</div>
                <p className="text-dim" style={{ margin: '0 0 12px', fontSize: 13, lineHeight: 1.5 }}>
                  If Bazarr and Verifyarr run in different containers, they may see the same folder under different names. Tell Verifyarr what Bazarr&apos;s path is called here. Leave empty if both see the same paths.
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto minmax(0,1fr)', gap: 8, alignItems: 'end', maxWidth: 560 }}>
                  <div>
                    <label htmlFor="mp-f">Path in Bazarr</label>
                    <input id="mp-f" type="text" placeholder="/tv" value={v.mapFrom} onChange={(e) => { setV({ mapFrom: e.target.value }); setMapTouched(true) }} style={{ fontFamily: 'var(--mono)', fontSize: 13 }} />
                  </div>
                  <span aria-hidden="true" className="text-dim" style={{ paddingBottom: 8 }}>→</span>
                  <div>
                    <label htmlFor="mp-t">Same folder in Verifyarr</label>
                    <input id="mp-t" type="text" placeholder="/media/tv" value={v.mapTo} onChange={(e) => { setV({ mapTo: e.target.value }); setMapTouched(true) }} style={{ fontFamily: 'var(--mono)', fontSize: 13 }} />
                  </div>
                </div>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                Verifyarr listens to the audio with speech recognition and compares it with each subtitle.
              </p>
              <div style={{ marginBottom: 16 }}>
                <RadioCards
                  name="speech"
                  value={v.speech}
                  onPick={(speech) => setV({ speech })}
                  options={[
                    { value: 'local', title: 'Local Whisper (recommended)', desc: 'Runs on this server. Free and private, no API key. Slower on small servers like an Intel N100, but it runs at night.' },
                    { value: 'cloud', title: 'Cloud provider', desc: 'Much faster. Short audio clips are sent to the provider; needs an API key and may cost money.' },
                  ]}
                />
              </div>
              {v.speech === 'cloud' && (
                <>
                  <div className="field">
                    <label htmlFor="cp">Provider</label>
                    <select id="cp" value={v.cloud} onChange={(e) => setV({ cloud: e.target.value as WizVals['cloud'] })}>
                      <option value="groq">Groq</option>
                      <option value="openrouter">OpenRouter</option>
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="ck">API key</label>
                    <input
                      id="ck"
                      type="password"
                      placeholder={v.cloudKeySet ? '••••••••••••••••  (saved — leave blank to keep)' : ''}
                      value={v.apiKey}
                      onChange={(e) => setV({ apiKey: e.target.value })}
                    />
                    <div className="field-hint" style={{ color: 'var(--text-dim)' }}>Stored on this server only. Short audio clips are sent to the provider.</div>
                  </div>
                </>
              )}
            </>
          )}

          {step === 4 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                Some subtitles can&apos;t be fixed: the wrong episode, missing parts, a cut version. What should Verifyarr do with them?
              </p>
              <RadioCards
                name="bad"
                value={v.badAction}
                onPick={(badAction) => setV({ badAction })}
                options={[
                  { value: 'off', title: 'Only flag', desc: 'Mark it here and leave the file alone.' },
                  { value: 'quarantine', title: 'Quarantine', desc: 'Move it out of the media folder so players stop showing it. You can restore it.' },
                  { value: 'blacklist', title: 'Blacklist in Bazarr', desc: 'Tell Bazarr it is bad so Bazarr downloads a different one.' },
                  { value: 'remediate', title: 'Fetch a replacement automatically', desc: 'Bazarr fetches candidates and Verifyarr tests each one. If none passes, the original is kept and stays flagged.' },
                ]}
              />
              {needsBazarr && (
                <p style={{ margin: '12px 0 0', fontSize: 13, color: 'var(--yellow)' }}>
                  <span aria-hidden="true">! </span>This needs Bazarr, which isn&apos;t connected yet. You can go back one step or add it later.
                </p>
              )}
            </>
          )}

          {step === 5 && (
            <>
              <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.5 }}>
                When should Verifyarr look for new and changed files? A scan works the processor hard, so night is best.{' '}
                Times are local time on the server{serverTz ? ` (${serverTz})` : ''}.
              </p>
              <RadioCards
                name="sched"
                value={v.schedMode}
                onPick={(schedMode) => setV({ schedMode })}
                options={[
                  { value: 'nightly', title: 'Every night', desc: 'Checks new and changed files once a day.' },
                  { value: 'weekly', title: 'Every week', desc: 'Checks new and changed files once a week.' },
                  ...(v.keepCron && parseCron(v.keepCron).mode === 'advanced'
                    ? [{ value: 'keep' as const, title: 'Keep current schedule', desc: `A custom schedule is set (${v.keepCron}); leave it alone.` }]
                    : []),
                ]}
              />
              {v.schedMode === 'nightly' && (
                <div className="field" style={{ marginTop: 14, maxWidth: 180 }}>
                  <label htmlFor="st">Start time</label>
                  <input id="st" type="time" value={v.schedTime} onChange={(e) => setV({ schedTime: e.target.value })} />
                </div>
              )}
              {v.schedMode === 'weekly' && (
                <div style={{ display: 'flex', gap: 12, marginTop: 14, maxWidth: 340 }}>
                  <div className="field" style={{ flex: 1 }}>
                    <label htmlFor="sd">Day</label>
                    <select id="sd" value={v.schedDay} onChange={(e) => setV({ schedDay: Number(e.target.value) })}>
                      {DAY_NAMES.map((d, i) => (
                        <option key={d} value={i}>{d}</option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ flex: 1 }}>
                    <label htmlFor="st2">Start time</label>
                    <input id="st2" type="time" value={v.schedTime} onChange={(e) => setV({ schedTime: e.target.value })} />
                  </div>
                </div>
              )}
              {!fromSettings && (
                <p className="text-dim" style={{ margin: '16px 0 0', fontSize: 13, lineHeight: 1.5 }}>
                  When you finish, the first scan starts right away. It checks the whole library and can take a few hours on a small server; you can use Verifyarr meanwhile.
                </p>
              )}
            </>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 22px', borderTop: '1px solid var(--border)', background: 'var(--bg-sidebar)', flexWrap: 'wrap' }}>
          <button type="button" onClick={() => navigate(fromSettings ? '/settings/general' : '/')} style={{ background: 'none', border: 0, color: 'var(--text-dim)', padding: '6px 0', fontSize: 13, cursor: 'pointer' }}>
            {fromSettings ? 'Cancel' : 'Skip setup'}
          </button>
          <span style={{ flex: 1 }} />
          {step > 0 && <button className="btn" type="button" onClick={() => go(step - 1)}>Back</button>}
          {step < 5 && <button className="btn" type="button" onClick={() => go(step + 1)}>Skip this step</button>}
          <button className="btn btn-primary" type="button" disabled={finishing} onClick={() => (step < 5 ? go(step + 1) : finish())}>
            {finishing ? <span className="spinner" /> : step < 5 ? 'Next' : fromSettings ? 'Save' : 'Finish and start first scan'}
          </button>
        </div>
      </div>
    </div>
  )
}
