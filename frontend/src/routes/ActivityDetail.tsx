import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { LogLine, RunRow } from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import CopyLogButton from '../components/CopyLogButton'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { useAutoScrollLog } from '../hooks/useAutoScrollLog'
import { useToasts } from '../hooks/useToasts'
import { durationBetween, formatClock, formatDuration, formatExact, formatRelative } from '../lib/format'
import { runTargetLabel, runTypeLabel } from '../lib/runLabels'

export default function ActivityDetail() {
  const { runId } = useParams()
  const [run, setRun] = useState<RunRow | null>(null)
  const [lines, setLines] = useState<LogLine[]>([])
  const [notFound, setNotFound] = useState(false)
  const [failed, setFailed] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [askCancel, setAskCancel] = useState(false)
  const { ref: logBoxRef, onScroll: onLogScroll, following, resume } = useAutoScrollLog(lines)
  const { toast } = useToasts()
  const navigate = useNavigate()

  useEffect(() => {
    setLines([])
    setRun(null)
    setNotFound(false)
    setFailed(false)
    api
      .get<RunRow>(`/runs/${runId}`)
      .then(setRun)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setNotFound(true)
        else setFailed(true)
      })

    const es = new EventSource(`/api/runs/${runId}/stream`)
    es.addEventListener('log', (ev) => {
      const line: LogLine = JSON.parse((ev as MessageEvent).data)
      setLines((prev) => [...prev, line])
    })
    es.addEventListener('done', (ev) => {
      const r: RunRow = JSON.parse((ev as MessageEvent).data)
      setRun(r)
      es.close()
    })
    es.addEventListener('error', () => {
      // EventSource retries the connection itself on ordinary network errors — we just fetch
      // run status separately as a safety net in case the connection dies completely.
      api.get<RunRow>(`/runs/${runId}`).then(setRun).catch(() => {})
    })
    return () => es.close()
  }, [runId])

  // Fallback poll of the run row itself (counts/status), independent of the SSE log lines.
  useEffect(() => {
    const id = setInterval(() => {
      api.get<RunRow>(`/runs/${runId}`).then((r) => {
        setRun(r)
        if (r.status !== 'running') clearInterval(id)
      }).catch(() => {})
    }, 2000)
    return () => clearInterval(id)
  }, [runId])

  async function cancel() {
    setAskCancel(false)
    setCancelling(true)
    try {
      await api.post(`/runs/${runId}/cancel`)
      toast(`Job #${runId} cancelled. Files already processed keep their results.`, { kind: 'info' })
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setCancelling(false)
    }
  }

  function goBack() {
    const idx = (window.history.state as { idx?: number } | null)?.idx
    if (typeof idx === 'number' && idx > 0) navigate(-1)
    else navigate('/activity')
  }

  if (failed) return <ErrorState title="Job" noun="this job" onRetry={() => navigate(0)} />
  if (!run && !notFound) return <LoadingState title="Job" noun="this job" />

  const running = run?.status === 'running'
  const prog = run && run.files_total ? run.files_processed / run.files_total : 0
  const pct = run && run.files_total ? Math.round(prog * 100) : null
  const elapsedSec = run ? Math.max(0, (Date.now() - new Date(run.started_at).getTime()) / 1000) : 0
  const leftSec = running && prog > 0.02 ? Math.max(1, elapsedSec / prog - elapsedSec) : null
  const memoryHint = !!run?.error_message && /memory|model/i.test(run.error_message)

  return (
    <div>
      {askCancel && run && (
        <ConfirmDialog
          title={`Cancel job #${run.id}?`}
          message={run.files_total
            ? `${run.files_processed} of ${run.files_total} files are done and keep their results. The rest are not checked until the next scan.`
            : `${run.files_processed} files are done and keep their results. The rest are not checked until the next scan.`}
          confirmLabel="Cancel job"
          danger
          onConfirm={cancel}
          onCancel={() => setAskCancel(false)}
        />
      )}
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <button className="btn btn-sm" onClick={goBack}>‹ Back</button>
        <h1 style={{ margin: '0 0 0 4px', fontSize: 17, flex: '1 1 220px', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {run ? `Job #${run.id} · ${runTypeLabel(run)}` : 'Job'}
        </h1>
        {running && (
          <button className="btn btn-danger" disabled={cancelling} onClick={() => setAskCancel(true)}>
            {cancelling ? <span className="spinner" /> : 'Cancel job'}
          </button>
        )}
      </div>

      {notFound && (
        <div data-content style={{ padding: 20 }}>
          <div className="card" style={{ maxWidth: 520 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>Job not found</h2>
            <p className="text-dim" style={{ margin: '0 0 12px' }}>There is no job with this number.</p>
            <button className="btn" onClick={() => navigate('/activity')}>Go to Activity</button>
          </div>
        </div>
      )}

      {run && (
        <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1100 }}>
          {run.status === 'failed' && run.error_message && (
            <div className="error-banner" role="alert" style={{ margin: 0 }}>
              <strong>This job stopped with an error.</strong> {run.error_message}{' '}
              {memoryHint && (
                <>Try a smaller model in <button onClick={() => navigate('/settings/correctness')} style={{ background: 'none', border: 0, padding: 0, color: '#ffb4b4', textDecoration: 'underline', cursor: 'pointer', fontSize: 'inherit' }}>Settings → Speech recognition</button>, then run the scan again.</>
              )}
            </div>
          )}

          <section className="card" aria-label="Progress">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
              <StatusPill value={run.status} />
              <span style={{ fontWeight: 600 }}>{runTargetLabel(run)}</span>
              <span className="text-dim" style={{ fontSize: 13, marginLeft: 'auto' }}>
                Started <span title={formatExact(run.started_at)}>{formatRelative(run.started_at)}</span>
                {' · '}{running ? `running for ${durationBetween(run.started_at, null)}` : `took ${durationBetween(run.started_at, run.finished_at)}`}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 6 }}>
              <span>
                {running && !run.files_total
                  ? 'Finding files to check…'
                  : running
                  ? `${run.files_processed}${run.files_total ? ` of ${run.files_total}` : ''} files${leftSec !== null ? ` · about ${formatDuration(leftSec)} left` : ''}`
                  : `${run.files_processed}${run.files_total ? ` of ${run.files_total}` : ''} files processed`}
              </span>
              {pct !== null && <span style={{ fontWeight: 600 }}>{pct}%</span>}
            </div>
            {pct !== null && (
              <div role="progressbar" aria-label="Job progress" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} style={{ height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
                <svg aria-hidden="true" width="100%" height="8" style={{ display: 'block' }}>
                  <rect
                    x="0" y="0" width={`${pct}%`} height="8"
                    style={{ fill: run.status === 'failed' ? 'var(--red)' : run.status === 'cancelled' ? 'var(--yellow)' : 'var(--accent)' }}
                  />
                </svg>
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(110px,1fr))', gap: 10, marginTop: 14 }}>
              {[
                { label: 'Processed', n: run.files_processed, bad: false },
                { label: 'Changed', n: run.files_changed, bad: false },
                { label: 'Flagged', n: run.files_suspect, bad: run.files_suspect > 0 },
                { label: 'Errors', n: run.files_error, bad: run.files_error > 0 },
                { label: 'Generated', n: run.files_generated, bad: false },
              ].map((c) => (
                <div key={c.label} style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '9px 11px' }}>
                  <div className="text-dim" style={{ fontSize: 12 }}>{c.label}</div>
                  <div style={{ fontSize: 20, fontWeight: 700, fontVariantNumeric: 'tabular-nums', color: c.bad ? 'var(--red)' : undefined }}>{c.n}</div>
                </div>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
              <button
                className="btn btn-sm"
                disabled={!run.files_changed}
                onClick={() => navigate(`/files?run_id=${run.id}&which=changed&sync_kind=fixed`)}
              >
                Files it changed ({run.files_changed})
              </button>
              <button
                className="btn btn-sm"
                disabled={!run.files_suspect}
                onClick={() => navigate(`/files?run_id=${run.id}&which=flagged&flag=attention`)}
              >
                Files it flagged ({run.files_suspect})
              </button>
            </div>
          </section>

          <section className="card" aria-labelledby="h-log" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderBottom: '1px solid var(--border)', flexWrap: 'wrap' }}>
              <h2 id="h-log" style={{ margin: 0, fontSize: 15 }}>Log</h2>
              <span className="text-dim" style={{ fontSize: 12.5, flex: 1 }}>
                {running
                  ? following ? 'Following new lines' : 'Paused while you read · new lines keep arriving'
                  : `${lines.length} lines`}
              </span>
              {!following && (
                <button className="btn btn-sm" onClick={resume}>Jump to latest</button>
              )}
              <CopyLogButton lines={lines} />
            </div>
            <div ref={logBoxRef} onScroll={onLogScroll} tabIndex={0} aria-label="Job log" role="log" style={{ height: 360, overflow: 'auto', background: 'var(--bg)', padding: '8px 0', fontFamily: 'var(--mono)', fontSize: 12.5, lineHeight: 1.6 }}>
              {lines.length === 0 && <div className="text-faint" style={{ padding: '0 14px' }}>{run.status === 'running' ? 'Waiting for log lines…' : 'This job wrote no log lines.'}</div>}
              {lines.map((l) => (
                <div key={l.id} style={{ display: 'grid', gridTemplateColumns: '70px 52px minmax(0,1fr)', gap: 10, padding: '0 14px' }}>
                  <span className="text-dim">{formatClock(l.ts)}</span>
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
        </div>
      )}
    </div>
  )
}
