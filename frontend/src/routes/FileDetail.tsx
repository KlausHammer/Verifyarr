import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { CorrectnessHistoryRow, FileRow } from '../api/types'
import { formatDateTime, formatBytes } from '../lib/format'
import StatusPill from '../components/StatusPill'

interface DetailResponse {
  file: FileRow
  correctness_history: CorrectnessHistoryRow[]
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', padding: '7px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
      <div style={{ width: 200, color: 'var(--text-dim)', flexShrink: 0 }}>{label}</div>
      <div style={{ wordBreak: 'break-all' }}>{children}</div>
    </div>
  )
}

export default function FileDetail() {
  const { id } = useParams()
  const [data, setData] = useState<DetailResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionMsg, setActionMsg] = useState<string | null>(null)

  function load() {
    api
      .get<DetailResponse>(`/files/${id}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [id])

  async function runSingle() {
    setBusy(true)
    setActionMsg(null)
    try {
      const r = await api.post<{ run_id: number }>(`/files/${id}/run-single`)
      setActionMsg(`Job #${r.run_id} started.`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function remediate() {
    setBusy(true)
    setActionMsg(null)
    try {
      const r = await api.post<{ run_id: number; result: string }>(`/files/${id}/remediate`)
      setActionMsg(r.result)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (error) return <div className="error-banner">{error}</div>
  if (!data) return <span className="spinner" />

  const f = data.file

  return (
    <div>
      <Link to="/files" className="text-dim">
        ← Files
      </Link>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '10px 0 18px' }}>
        <h1 style={{ margin: 0 }}>
          {f.season_episode ?? f.series_or_movie_title ?? 'File'} <span className="text-dim">({f.lang})</span>
        </h1>
        <div style={{ display: 'flex', gap: 8 }}>
          {f.subtitle_path && (
            <button className="btn" disabled={busy} onClick={runSingle}>
              Run now
            </button>
          )}
          {f.correctness_flag === 'SUSPECT' && (
            <button className="btn btn-danger" disabled={busy} onClick={remediate}>
              Remediate from Bazarr
            </button>
          )}
        </div>
      </div>
      {actionMsg && <div className="card" style={{ marginBottom: 16, fontSize: 13 }}>{actionMsg}</div>}

      <div className="card" style={{ marginBottom: 20 }}>
        <Row label="Video">{f.video_path}</Row>
        <Row label="Subtitle">{f.subtitle_path ?? <span className="text-faint">missing</span>}</Row>
        <Row label="Language">{f.lang ?? '—'}</Row>
        <Row label="Sync status">
          <StatusPill value={f.sync_status} /> {f.sync_max_shift_s !== null && `Δ${f.sync_max_shift_s}s`}
          {f.sync_split_blocks !== null && f.sync_split_blocks > 1 && ` · ${f.sync_split_blocks} blocks`}
        </Row>
        <Row label="Correctness">
          <StatusPill value={f.correctness_flag} />{' '}
          {f.correctness_avg_score !== null && <span className="mono">score {f.correctness_avg_score}</span>}
        </Row>
        <Row label="Auto action">{f.auto_action ?? '—'}</Row>
        <Row label="Note">{f.note || '—'}</Row>
        <Row label="Video size">{formatBytes(f.video_size)}</Row>
        <Row label="Subtitle size">{formatBytes(f.subtitle_size)}</Row>
        <Row label="Last processed">{formatDateTime(f.last_processed)}</Row>
      </div>

      <h3>Correctness history</h3>
      <div className="card" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Time</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Flag</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Score</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Audio language</th>
            </tr>
          </thead>
          <tbody>
            {data.correctness_history.length === 0 && (
              <tr>
                <td colSpan={4} style={{ padding: '9px 12px' }} className="text-dim">
                  No history yet.
                </td>
              </tr>
            )}
            {data.correctness_history.map((h) => (
              <tr key={h.id} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '9px 12px' }}>{formatDateTime(h.checked_at)}</td>
                <td style={{ padding: '9px 12px' }}>
                  <StatusPill value={h.correctness_flag} />
                </td>
                <td style={{ padding: '9px 12px' }} className="mono">
                  {h.correctness_avg_score ?? '—'}
                </td>
                <td style={{ padding: '9px 12px' }}>{h.audio_lang ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
