import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { BlacklistAction, Paginated } from '../api/types'
import { fileName, formatDateTime } from '../lib/format'

const PAGE_SIZE = 40

export default function BazarrBlacklist() {
  const [data, setData] = useState<Paginated<BlacklistAction> | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .get<Paginated<BlacklistAction>>(`/bazarr/blacklist?page=${page}&page_size=${PAGE_SIZE}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [page])

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <div>
      <h1>Bazarr blacklist</h1>
      <p className="text-dim" style={{ maxWidth: 640, marginBottom: 16 }}>
        Everything verifyarr has told Bazarr to blacklist, because a subtitle's content didn't
        match the video's speech. Controlled by the "Action if SUSPECT" column on Settings →
        Automation — Correctness check and/or Line-order check need to be set to blacklist or
        remediate for anything to show up here.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>File</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Language</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Provider</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Outcome</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Time</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.length === 0 && (
              <tr>
                <td colSpan={5} style={{ padding: '9px 12px' }} className="text-dim">
                  Empty.
                </td>
              </tr>
            )}
            {data?.items.map((b) => (
              <tr key={b.id} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '9px 12px' }}>{fileName(b.subtitle_path)}</td>
                <td style={{ padding: '9px 12px' }}>{b.language ?? '—'}</td>
                <td style={{ padding: '9px 12px' }}>{b.provider ?? '—'}</td>
                <td style={{ padding: '9px 12px' }} className="text-dim">
                  {b.remediation_outcome ?? '—'}
                </td>
                <td style={{ padding: '9px 12px' }} className="text-dim">
                  {formatDateTime(b.blacklisted_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
        <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          ← Previous
        </button>
        <span className="text-dim" style={{ alignSelf: 'center' }}>
          Page {page} of {totalPages}
        </span>
        <button className="btn btn-sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
          Next →
        </button>
      </div>
    </div>
  )
}
