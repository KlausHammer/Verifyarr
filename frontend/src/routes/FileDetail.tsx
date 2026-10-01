import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { CorrectnessHistoryRow, FileRow } from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { fileName, formatBytes, formatExact, formatRelative } from '../lib/format'
import { languageName } from '../lib/languages'
import { GENERATE_UI } from '../lib/features'
import { runMatchesFile } from '../lib/runLabels'
import { changeLines, fileDisplayName, historyVerdict, verdict, type VerdictAction } from '../lib/verdict'

interface DetailResponse {
  file: FileRow
  correctness_history: CorrectnessHistoryRow[]
}

interface PendingConfirm {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
}

function leadLine(f: FileRow): string {
  if (f.series_or_movie_title && f.season_episode) {
    const m = f.season_episode.match(/^S(\d+)E(\d+)$/i)
    if (m) return `${f.series_or_movie_title} · Season ${Number(m[1])}, episode ${Number(m[2])}`
    return `${f.series_or_movie_title} · ${f.season_episode}`
  }
  if (f.series_or_movie_title) return `${f.series_or_movie_title} · movie`
  return fileName(f.video_path)
}

export default function FileDetail() {
  const { id } = useParams()
  const [data, setData] = useState<DetailResponse | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null)
  const { run: runningRun, isRunning, refresh: refreshRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  const load = useCallback(async () => {
    setFailed(false)
    setNotFound(false)
    try {
      setData(await api.get<DetailResponse>(`/files/${id}`))
    } catch (err) {
      setData(null)
      if (err instanceof ApiError && err.status === 404) setNotFound(true)
      else setFailed(true)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  function goBack() {
    const idx = (window.history.state as { idx?: number } | null)?.idx
    if (typeof idx === 'number' && idx > 0) navigate(-1)
    else navigate('/files')
  }

  if (failed) return <ErrorState title="File" noun="this file" onRetry={load} />
  if (!data && !notFound) return <LoadingState title="File" noun="this file" />

  const f = data?.file ?? null
  const history = data?.correctness_history ?? []
  const v = f ? verdict(f) : null
  const nosub = f !== null && (f.sync_status === 'missing' || !f.subtitle_path)
  const targeted = f !== null && runningRun !== null && runMatchesFile(runningRun, f)
  const audioLang = history.find((h) => h.audio_lang)?.audio_lang ?? null

  async function doRecheck() {
    setBusy('recheck')
    try {
      const r = await api.post<{ run_id: number }>(`/files/${id}/run-single`)
      refreshRunning()
      toast('Re-check started.', { kind: 'info', action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job' })
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  // Synchronous server-side (unlike re-check): the button spins until Bazarr answers.
  async function doReplace() {
    setBusy('replace')
    try {
      const r = await api.post<{ run_id: number; result: string }>(`/files/${id}/remediate`)
      toast(r.result, { action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job' })
      load()
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  async function doGenerate() {
    setBusy('generate')
    try {
      const r = await api.post<{ run_id: number }>(`/files/${id}/generate`)
      refreshRunning()
      toast('Generating subtitle.', { kind: 'info', action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job' })
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  async function doQuarantine() {
    setBusy('quarantine')
    try {
      await api.post(`/files/${id}/quarantine`)
      toast('Moved to quarantine.', { action: () => navigate('/quarantine'), actionLabel: 'View' })
      load()
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  async function doBlacklist() {
    setBusy('blacklist')
    try {
      await api.post(`/files/${id}/blacklist`)
      toast('Blacklisted in Bazarr. Bazarr will look for another subtitle.', {
        action: () => navigate('/bazarr-blacklist'), actionLabel: 'View',
      })
      load()
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  function runAction(action: VerdictAction) {
    if (!f) return
    if (action === 'replace') doReplace()
    else if (action === 'recheck') doRecheck()
    else if (action === 'generate') askGenerate()
    else navigate('/settings/general')
  }

  function askGenerate() {
    if (!f) return
    setConfirm({
      title: 'Generate a subtitle with Whisper?',
      message: `Whisper transcribes the whole audio of ${fileDisplayName(f)} and writes a ${f.lang ? languageName(f.lang) : ''} subtitle. On a small server this can take 10–30 minutes.`,
      confirmLabel: 'Generate',
      onConfirm: doGenerate,
    })
  }

  const name = f ? fileDisplayName(f) : 'File'
  const buttonsDisabled = busy !== null || isRunning

  return (
    <div>
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          confirmLabel={confirm.confirmLabel}
          danger={confirm.danger}
          onConfirm={() => { setConfirm(null); confirm.onConfirm() }}
          onCancel={() => setConfirm(null)}
        />
      )}
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <button className="btn btn-sm" onClick={goBack}>‹ Back</button>
        <h1 style={{ margin: '0 0 0 4px', fontSize: 17, flex: '1 1 220px', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {name}
        </h1>
        {f && (
          <>
            <button className="btn" onClick={doRecheck} disabled={buttonsDisabled || nosub}>
              {busy === 'recheck' ? <span className="spinner" /> : 'Re-check'}
            </button>
            <button className="btn" onClick={doReplace} disabled={buttonsDisabled || nosub}>
              {busy === 'replace' ? <span className="spinner" /> : 'Fetch replacement'}
            </button>
            {GENERATE_UI && (
              <button
                className="btn"
                onClick={askGenerate}
                disabled={buttonsDisabled || !nosub}
                title={nosub ? '' : 'Only for videos without a subtitle in this language'}
              >
                {busy === 'generate' ? <span className="spinner" /> : 'Generate with Whisper'}
              </button>
            )}
            <button className="btn btn-danger" disabled={buttonsDisabled || nosub} onClick={() => setConfirm({
              title: 'Quarantine this subtitle?',
              message: `${fileName(f.subtitle_path)} is moved out of the media folder so players stop showing it. You can restore it from Quarantine & Backups.`,
              confirmLabel: 'Quarantine',
              danger: true,
              onConfirm: doQuarantine,
            })}>
              {busy === 'quarantine' ? <span className="spinner" /> : 'Quarantine'}
            </button>
            <button className="btn btn-danger" disabled={buttonsDisabled || nosub} onClick={() => setConfirm({
              title: 'Blacklist in Bazarr?',
              message: 'Bazarr will not download this subtitle again and will look for another one. The file stays until Bazarr replaces it.',
              confirmLabel: 'Blacklist',
              danger: true,
              onConfirm: doBlacklist,
            })}>
              {busy === 'blacklist' ? <span className="spinner" /> : 'Blacklist'}
            </button>
          </>
        )}
      </div>

      {notFound && (
        <div data-content style={{ padding: 20 }}>
          <div className="card" style={{ maxWidth: 560 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>File not found</h2>
            <p className="text-dim" style={{ margin: '0 0 12px' }}>It may have been renamed or removed since the last scan.</p>
            <button className="btn" onClick={() => navigate('/files')}>Go to Files</button>
          </div>
        </div>
      )}

      {f && v && (
        <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1100 }}>
          {targeted && runningRun && (
            <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', border: '1px solid #2f4a70', background: 'rgba(110,168,254,.08)', borderRadius: 'var(--radius)', fontSize: 13 }}>
              <span className="spinner" style={{ width: 12, height: 12 }} />
              <span style={{ flex: 1 }}>
                {runningRun.mode === 'generate_single' ? 'Generating a subtitle' : 'A check is running'} for this file
                {runningRun.files_total ? ` (${Math.round((runningRun.files_processed / runningRun.files_total) * 100)}%)` : ''}.
                The result appears here when it finishes.
              </span>
              <button className="btn btn-sm" onClick={() => navigate(`/activity/${runningRun.id}`)}>View job</button>
            </div>
          )}
          {isRunning && !targeted && runningRun && (
            <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', border: '1px solid #2f4a70', background: 'rgba(110,168,254,.08)', borderRadius: 'var(--radius)', fontSize: 13 }}>
              <span className="spinner" style={{ width: 12, height: 12 }} />
              <span style={{ flex: 1 }}>A job is running. The buttons above unlock when it finishes.</span>
              <button className="btn btn-sm" onClick={() => navigate(`/activity/${runningRun.id}`)}>View job</button>
            </div>
          )}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 18px', fontSize: 13, color: 'var(--text-dim)' }}>
            <span><span style={{ color: 'var(--text)', fontWeight: 600 }}>{leadLine(f)}</span></span>
            <span>Subtitle: <span style={{ color: 'var(--text)' }}>{f.lang ? languageName(f.lang) : '—'}</span></span>
            <span>Audio: <span style={{ color: 'var(--text)' }}>{audioLang ? languageName(audioLang) : '—'}</span></span>
            <span>Last processed: <span style={{ color: 'var(--text)' }} title={formatExact(f.last_processed)}>{formatRelative(f.last_processed)}</span></span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,320px),1fr))', gap: 16 }}>
            <section className="card" aria-labelledby="h-v" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <h2 id="h-v" style={{ margin: 0, fontSize: 15 }}>Result</h2>
                <span className={`pill ${v.cls}`} style={{ fontSize: 13, padding: '3px 11px' }}>
                  <span aria-hidden="true">{v.icon}</span>{v.label}
                </span>
              </div>
              <div>
                <div className="text-dim" style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 2 }}>What happened</div>
                <div style={{ fontSize: 15, lineHeight: 1.5 }}>{v.happened}</div>
              </div>
              <div>
                <div className="text-dim" style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 2 }}>What to do</div>
                <div style={{ fontSize: 15, lineHeight: 1.5 }}>{v.todo}</div>
              </div>
              {v.action && (
                <div>
                  <button className="btn btn-primary" onClick={() => runAction(v.action!)} disabled={buttonsDisabled || (v.action === 'generate' && !nosub)}>
                    {v.key === 'repl_failed' ? 'Try another replacement' : v.actionLabel}
                  </button>
                </div>
              )}
            </section>

            <section className="card" aria-labelledby="h-c">
              <h2 id="h-c" style={{ margin: '0 0 10px', fontSize: 15 }}>What changed</h2>
              {changeLines(f).length === 0 ? (
                <p className="text-dim" style={{ margin: 0 }}>Nothing was changed in this subtitle.</p>
              ) : (
                <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {changeLines(f).map((c) => (
                    <li key={c} style={{ display: 'flex', gap: 8 }}>
                      <span aria-hidden="true" style={{ color: 'var(--green)' }}>✓</span>{c}
                    </li>
                  ))}
                </ul>
              )}
              <div style={{ borderTop: '1px solid var(--border)', marginTop: 12, paddingTop: 10, fontSize: 13 }}>
                <span className="text-dim">Automatic action: </span>{f.auto_action ?? '—'}
              </div>
            </section>
          </div>

          <section className="card" aria-labelledby="h-p" style={{ display: 'grid', gridTemplateColumns: 'auto minmax(0,1fr) auto', gap: '6px 14px', fontSize: 13, alignItems: 'baseline' }}>
            <h2 id="h-p" style={{ gridColumn: '1/-1', margin: '0 0 4px', fontSize: 15 }}>Files</h2>
            <span className="text-dim">Video</span>
            <span className="mono" style={{ wordBreak: 'break-all', fontSize: 12.5 }}>{f.video_path}</span>
            <span className="text-dim">{formatBytes(f.video_size)}</span>
            <span className="text-dim">Subtitle</span>
            <span className="mono" style={{ wordBreak: 'break-all', fontSize: 12.5 }}>
              {f.subtitle_path ?? 'No subtitle file for this language'}
            </span>
            <span className="text-dim">{f.subtitle_path ? formatBytes(f.subtitle_size) : ''}</span>
          </section>

          <details className="card" style={{ padding: 0 }}>
            <summary style={{ padding: '12px 16px', cursor: 'pointer', fontWeight: 600 }}>Technical details</summary>
            <div style={{ padding: '0 16px 14px', display: 'flex', flexDirection: 'column', gap: 10, fontSize: 13 }}>
              <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'center' }}>
                <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span className="text-dim">Sync result</span><StatusPill value={f.sync_status} />
                </span>
                <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span className="text-dim">Check result</span><StatusPill value={f.correctness_flag} />
                </span>
                <span><span className="text-dim">Score </span>{f.correctness_avg_score === null ? '—' : f.correctness_avg_score.toFixed(2)}</span>
                <span>
                  <span className="text-dim">Swapped lines </span>
                  {(f.line_order_flagged ?? 0) > 0 ? `${f.line_order_flagged} flagged`
                    : (f.line_order_fixed ?? 0) > 0 ? `${f.line_order_fixed} fixed`
                    : f.line_order_flagged === null && f.line_order_fixed === null ? '—' : 'none'}
                </span>
              </div>
              <div className="mono" style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '10px 12px', fontSize: 12.5, lineHeight: 1.55, wordBreak: 'break-word' }}>
                {f.sync_status ?? '—'} · {f.correctness_flag ?? '—'}{f.reason ? ` (${f.reason})` : ''}
                {f.note ? <><br />{f.note}</> : null}
              </div>
            </div>
          </details>

          <section className="card" aria-labelledby="h-h" style={{ padding: 0, overflow: 'hidden' }}>
            <h2 id="h-h" style={{ margin: 0, padding: '14px 16px 10px', fontSize: 15 }}>Check history</h2>
            <div role="table" aria-labelledby="h-h">
              <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.2fr) minmax(0,1.4fr) 80px minmax(0,1fr)', gap: 12, padding: '7px 16px', borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
                <div role="columnheader">Time</div>
                <div role="columnheader">Result</div>
                <div role="columnheader" style={{ textAlign: 'right' }}>Score</div>
                <div role="columnheader">Audio language</div>
              </div>
              {history.length === 0 && (
                <div className="text-dim" style={{ padding: '10px 16px 14px', fontSize: 13 }}>No checks recorded yet.</div>
              )}
              {history.map((h) => {
                const hv = historyVerdict(h.correctness_flag)
                return (
                  <div key={h.id} role="row" data-row style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.2fr) minmax(0,1.4fr) 80px minmax(0,1fr)', gap: 12, alignItems: 'center', padding: '7px 16px', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                    <div role="cell" data-cell data-label="Time" title={formatExact(h.checked_at)}>{formatRelative(h.checked_at)}</div>
                    <div role="cell" data-cell data-label="Result">
                      <span className={`pill ${hv.cls}`}><span aria-hidden="true">{hv.icon}</span>{hv.label}</span>
                    </div>
                    <div role="cell" data-cell data-label="Score" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {h.correctness_avg_score === null ? '—' : h.correctness_avg_score.toFixed(2)}
                    </div>
                    <div role="cell" data-cell data-label="Audio language">
                      {h.audio_lang ? languageName(h.audio_lang) : '—'}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
