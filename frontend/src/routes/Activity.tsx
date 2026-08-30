import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { Paginated, RunRow } from '../api/types'
import { formatDateTime, durationBetween } from '../lib/format'
import { runTypeLabel, runTargetLabel } from '../lib/runLabels'
import StatusPill from '../components/StatusPill'

const PAGE_SIZE = 25

// No "run sweep" button here on purpose -- a sweep always needs a scope (Movies, Series, one
// title, or one season), and Scan/Rescan on the Movies/Series pages already cover that. This
// page is history/monitoring only: see runs, drill into one, cancel one in progress.
export default function Activity() {
  const [params, setParams] = useSearchParams()
  const page = Number(params.get('page') ?? '1')
  const [data, setData] = useState<Paginated<RunRow> | null>(null)
  const [error, setError] = useState<string | null>(null)

  function load() {
    api
      .get<Paginated<RunRow>>(`/runs?page=${page}&page_size=${PAGE_SIZE}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [page])
  useEffect(() => {
    const id = setInterval(load, 4000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page])

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <div>
      <h1 style={{ marginBottom: 16 }}>Activity</h1>
      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>#</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Type</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Target</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Status</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Files</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Duration</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Started</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.map((r) => (
              <tr key={r.id} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '9px 12px' }}>
                  <Link to={`/activity/${r.id}`}>#{r.id}</Link>
                </td>
                <td style={{ padding: '9px 12px' }}>{runTypeLabel(r)}</td>
                <td style={{ padding: '9px 12px' }}>{runTargetLabel(r)}</td>
                <td style={{ padding: '9px 12px' }}>
                  <StatusPill value={r.status} />
                </td>
                <td style={{ padding: '9px 12px' }}>
                  {r.files_processed}
                  {r.files_total ? ` / ${r.files_total}` : ''}
                  {r.files_suspect > 0 && <span style={{ color: 'var(--red)' }}> · {r.files_suspect} suspect</span>}
                </td>
                <td style={{ padding: '9px 12px' }}>{durationBetween(r.started_at, r.finished_at)}</td>
                <td style={{ padding: '9px 12px' }} className="text-dim">
                  {formatDateTime(r.started_at)}
                </td>
              </tr>
            ))}
            {data?.items.length === 0 && (
              <tr>
                <td colSpan={7} style={{ padding: '9px 12px' }} className="text-dim">
                  No runs yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
        <button
          className="btn btn-sm"
          disabled={page <= 1}
          onClick={() => setParams({ page: String(page - 1) })}
        >
          ← Previous
        </button>
        <span className="text-dim" style={{ alignSelf: 'center' }}>
          Page {page} of {totalPages}
        </span>
        <button
          className="btn btn-sm"
          disabled={page >= totalPages}
          onClick={() => setParams({ page: String(page + 1) })}
        >
          Next →
        </button>
      </div>
    </div>
  )
}
