import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { Paginated, RunRow } from '../api/types'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { durationBetween, formatExact, formatRelative } from '../lib/format'
import { runTargetLabel, runTypeLabel } from '../lib/runLabels'

const PAGE_SIZE = 25

export default function Activity() {
  const [params, setParams] = useSearchParams()
  const [data, setData] = useState<Paginated<RunRow> | null>(null)
  const [failed, setFailed] = useState(false)
  const [starting, setStarting] = useState(false)
  const { isRunning, refresh: refreshRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  const page = Math.max(1, Number(params.get('page') ?? '1') || 1)
  const filter = params.get('status') ?? ''

  function load() {
    setFailed(false)
    const qs = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (filter) qs.set('status', filter)
    api
      .get<Paginated<RunRow>>(`/runs?${qs.toString()}`)
      .then(setData)
      .catch(() => {
        setData(null)
        setFailed(true)
      })
  }

  useEffect(load, [page, filter])
  useEffect(() => {
    const id = setInterval(load, 4000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, filter])

  function setFilter(value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set('status', value)
    else next.delete('status')
    next.delete('page')
    setParams(next)
  }

  async function scan() {
    setStarting(true)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep' })
      refreshRunning()
      load()
      toast('Scan started.', { kind: 'info', action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job' })
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setStarting(false)
    }
  }

  if (failed) return <ErrorState title="Activity" noun="jobs" onRetry={load} />
  if (!data) return <LoadingState title="Activity" noun="jobs" />

  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))
  const cur = Math.min(totalPages, page)

  return (
    <div>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17 }}>Activity</h1>
        <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>{data.total ? `${data.total} jobs` : ''}</span>
        <div style={{ width: 150 }}>
          <select aria-label="Show jobs" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">All jobs</option>
            <option value="running">Running</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>
        <button className="btn btn-primary" onClick={scan} disabled={isRunning || starting}>
          {isRunning ? 'Scan running…' : starting ? <span className="spinner" /> : 'Scan library'}
        </button>
      </div>

      <div data-content style={{ padding: 20 }}>
        {data.items.length === 0 && (
          <div className="card" style={{ maxWidth: 600 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>{filter ? 'No jobs with this status' : 'No jobs yet'}</h2>
            <p className="text-dim" style={{ margin: 0, lineHeight: 1.5 }}>
              {filter
                ? 'Choose another status to see more jobs.'
                : 'Jobs appear here when Verifyarr scans the library, re-checks a subtitle or fetches a replacement. The first one starts when you press Scan library.'}
            </p>
          </div>
        )}

        {data.items.length > 0 && (
          <>
            <div role="table" aria-label="Jobs" className="card" style={{ padding: 0, overflowX: 'auto' }}>
              <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: '52px minmax(0,1.2fr) minmax(0,2fr) 100px 104px 96px 76px 70px 64px 56px', minWidth: 860, gap: 12, padding: '8px 14px', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
                <div role="columnheader">#</div>
                <div role="columnheader">Type</div>
                <div role="columnheader">Target</div>
                <div role="columnheader">Status</div>
                <div role="columnheader">Started</div>
                <div role="columnheader">Duration</div>
                <div role="columnheader" style={{ textAlign: 'right' }}>Processed</div>
                <div role="columnheader" style={{ textAlign: 'right' }}>Changed</div>
                <div role="columnheader" style={{ textAlign: 'right' }}>Flagged</div>
                <div role="columnheader" style={{ textAlign: 'right' }}>Errors</div>
              </div>
              {data.items.map((j) => {
                const running = j.status === 'running'
                const pct = running && j.files_total
                  ? Math.round((j.files_processed / j.files_total) * 100)
                  : null
                return (
                  <div key={j.id} role="row" data-row data-hover style={{ display: 'grid', gridTemplateColumns: '52px minmax(0,1.2fr) minmax(0,2fr) 100px 104px 96px 76px 70px 64px 56px', minWidth: 860, gap: 12, alignItems: 'center', padding: '7px 14px', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                    <div role="cell" data-cell data-label="Job" className="text-dim" style={{ fontVariantNumeric: 'tabular-nums' }}>#{j.id}</div>
                    <div role="cell" data-cell="main" style={{ minWidth: 0 }}>
                      <button
                        onClick={() => navigate(`/activity/${j.id}`)}
                        style={{ background: 'none', border: 0, padding: 0, color: 'var(--text)', fontWeight: 500, fontSize: 14, textAlign: 'left', cursor: 'pointer' }}
                      >
                        {runTypeLabel(j)}
                      </button>
                    </div>
                    <div role="cell" data-cell data-label="Target" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-dim)' }}>
                      {runTargetLabel(j)}
                    </div>
                    <div role="cell" data-cell data-label="Status"><StatusPill value={j.status} /></div>
                    <div role="cell" data-cell data-label="Started" style={{ color: 'var(--text-dim)' }} title={formatExact(j.started_at)}>
                      {formatRelative(j.started_at)}
                    </div>
                    <div role="cell" data-cell data-label="Duration" className={running ? '' : 'text-dim'}>
                      {running && pct !== null ? `${pct}% · ` : ''}{durationBetween(j.started_at, j.finished_at)}
                    </div>
                    <div role="cell" data-cell data-label="Processed" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {running && j.files_total ? `${j.files_processed}/${j.files_total}` : j.files_processed}
                    </div>
                    <div role="cell" data-cell data-label="Changed" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{j.files_changed}</div>
                    <div role="cell" data-cell data-label="Flagged" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {j.files_suspect > 0 ? <span style={{ color: 'var(--red)' }}>{j.files_suspect}</span> : <span className="text-dim">0</span>}
                    </div>
                    <div role="cell" data-cell data-label="Errors" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {j.files_error > 0 ? <span style={{ color: 'var(--red)' }}>{j.files_error}</span> : <span className="text-dim">0</span>}
                    </div>
                  </div>
                )
              })}
            </div>
            {totalPages > 1 && (
              <nav aria-label="Pages" style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 12, flexWrap: 'wrap' }}>
                <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>
                  Showing {(cur - 1) * PAGE_SIZE + 1}–{Math.min(data.total, cur * PAGE_SIZE)} of {data.total}
                </span>
                <button className="btn btn-sm" disabled={cur <= 1} onClick={() => setParams({ ...Object.fromEntries(params), page: String(cur - 1) })}>‹ Previous</button>
                <span className="text-dim" style={{ fontSize: 13 }}>Page {cur} of {totalPages}</span>
                <button className="btn btn-sm" disabled={cur >= totalPages} onClick={() => setParams({ ...Object.fromEntries(params), page: String(cur + 1) })}>Next ›</button>
              </nav>
            )}
          </>
        )}
      </div>
    </div>
  )
}
