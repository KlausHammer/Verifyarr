import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ArchivedItem } from '../api/types'
import { formatBytes, formatDateTime } from '../lib/format'

interface ListResponse {
  items: ArchivedItem[]
  media_roots: string[]
}

function ArchiveTable({ endpoint, restoreEndpoint }: { endpoint: string; restoreEndpoint: string }) {
  const [data, setData] = useState<ListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  function load() {
    api
      .get<ListResponse>(endpoint)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [endpoint])

  async function restore(item: ArchivedItem) {
    setBusy(item.path)
    setError(null)
    setMsg(null)
    try {
      const body: { path: string; media_root?: string } = { path: item.path }
      if (data && data.media_roots.length === 1) body.media_root = data.media_roots[0]
      const r = await api.post<{ ok: boolean; restored_to: string }>(restoreEndpoint, body)
      setMsg(`Restored to ${r.restored_to}`)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      {msg && (
        <div className="card" style={{ marginBottom: 14, fontSize: 13 }}>
          {msg}
        </div>
      )}
      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Original filename</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Size</th>
              <th style={{ textAlign: 'left', padding: '9px 12px' }}>Time</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data?.items.length === 0 && (
              <tr>
                <td colSpan={4} style={{ padding: '9px 12px' }} className="text-dim">
                  Empty.
                </td>
              </tr>
            )}
            {data?.items.map((item) => (
              <tr key={item.path} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '9px 12px' }}>{item.original_name}</td>
                <td style={{ padding: '9px 12px' }}>{formatBytes(item.size)}</td>
                <td style={{ padding: '9px 12px' }} className="text-dim">
                  {formatDateTime(new Date(item.mtime * 1000).toISOString())}
                </td>
                <td style={{ padding: '9px 12px' }}>
                  <button className="btn btn-sm" disabled={busy === item.path} onClick={() => restore(item)}>
                    {busy === item.path ? <span className="spinner" /> : 'Restore'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Quarantine() {
  const [tab, setTab] = useState<'quarantine' | 'backups'>('quarantine')

  return (
    <div>
      <h1>Quarantine & Backup</h1>
      <p className="text-dim" style={{ maxWidth: 640 }}>
        Quarantine holds subtitles that were marked SUSPECT and moved out of the way. Backup
        holds the original, pre-sync version of every fixed subtitle. "Restore" moves/copies the
        file back to its original location.
      </p>
      <div style={{ display: 'flex', gap: 8, margin: '16px 0' }}>
        <button className={`btn ${tab === 'quarantine' ? 'btn-primary' : ''}`} onClick={() => setTab('quarantine')}>
          Quarantine
        </button>
        <button className={`btn ${tab === 'backups' ? 'btn-primary' : ''}`} onClick={() => setTab('backups')}>
          Backups
        </button>
      </div>
      {tab === 'quarantine' ? (
        <ArchiveTable endpoint="/quarantine" restoreEndpoint="/quarantine/restore" />
      ) : (
        <ArchiveTable endpoint="/backups" restoreEndpoint="/backups/restore" />
      )}
    </div>
  )
}
