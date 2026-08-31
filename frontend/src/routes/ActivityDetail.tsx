import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { LogLine, RunRow } from '../api/types'
import { durationBetween, formatDateTime } from '../lib/format'
import { runTypeLabel, runTargetLabel } from '../lib/runLabels'
import StatusPill from '../components/StatusPill'
import CopyLogButton from '../components/CopyLogButton'
import styles from './ActivityDetail.module.css'

export default function ActivityDetail() {
  const { runId } = useParams()
  const [run, setRun] = useState<RunRow | null>(null)
  const [lines, setLines] = useState<LogLine[]>([])
  const [error, setError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const logBoxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setLines([])
    setRun(null)
    api
      .get<RunRow>(`/runs/${runId}`)
      .then(setRun)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))

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

  useEffect(() => {
    if (logBoxRef.current) logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight
  }, [lines])

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
    setCancelling(true)
    try {
      await api.post(`/runs/${runId}/cancel`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div>
      <Link to="/activity" className="text-dim">
        ← Activity
      </Link>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '10px 0 18px' }}>
        <h1 style={{ margin: 0 }}>Run #{runId}</h1>
        {run?.status === 'running' && (
          <button className="btn btn-danger" disabled={cancelling} onClick={cancel}>
            {cancelling ? <span className="spinner" /> : 'Cancel'}
          </button>
        )}
      </div>
      {error && <div className="error-banner">{error}</div>}

      {run && (
        <div className={styles.meta}>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Status</div>
            <StatusPill value={run.status} />
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Type</div>
            {runTypeLabel(run)}
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Target</div>
            {runTargetLabel(run)}
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Files</div>
            {run.files_processed}
            {run.files_total ? ` / ${run.files_total}` : ''}
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Fixed / suspect / error</div>
            {run.files_changed} / {run.files_suspect} / {run.files_error}
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Duration</div>
            {durationBetween(run.started_at, run.finished_at)}
          </div>
          <div className={styles.metaItem}>
            <div className={styles.metaLabel}>Started</div>
            {formatDateTime(run.started_at)}
          </div>
        </div>
      )}
      {run?.error_message && <div className="error-banner">{run.error_message}</div>}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
        <CopyLogButton lines={lines} />
      </div>
      <div className={styles.logBox} ref={logBoxRef}>
        {lines.length === 0 && <div className="text-faint">Waiting for log lines…</div>}
        {lines.map((l) => (
          <div key={l.id} className={`${styles.logLine} ${styles[`level${l.level}`] ?? ''}`}>
            <span className={styles.ts}>{new Date(l.ts).toLocaleTimeString('en-US')}</span>
            {l.message}
          </div>
        ))}
      </div>
    </div>
  )
}
