import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ArchivedItem } from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import { ErrorState, LoadingState } from '../components/PageState'
import { useToasts } from '../hooks/useToasts'
import { formatBytes, formatExact, formatRelative } from '../lib/format'

interface ListResponse {
  items: ArchivedItem[]
  media_roots: string[]
}

function parentDir(relPath: string): string {
  const i = relPath.lastIndexOf('/')
  return i >= 0 ? relPath.slice(0, i) : ''
}

export default function Quarantine() {
  const [quarantine, setQuarantine] = useState<ListResponse | null>(null)
  const [backups, setBackups] = useState<ListResponse | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [restoreRoot, setRestoreRoot] = useState('')
  const [askBackup, setAskBackup] = useState<ArchivedItem | null>(null)
  const { toast } = useToasts()

  function load() {
    setFailed(false)
    Promise.all([api.get<ListResponse>('/quarantine'), api.get<ListResponse>('/backups')])
      .then(([q, b]) => {
        setQuarantine(q)
        setBackups(b)
        const roots = q.media_roots.length ? q.media_roots : b.media_roots
        setRestoreRoot((prev) => (prev && roots.includes(prev) ? prev : (roots[0] ?? '')))
      })
      .catch(() => {
        setQuarantine(null)
        setBackups(null)
        setFailed(true)
      })
  }

  useEffect(load, [])

  async function restore(item: ArchivedItem, endpoint: string) {
    setAskBackup(null)
    setBusy(`${endpoint}:${item.path}`)
    try {
      const body: { path: string; media_root?: string } = { path: item.path }
      if (restoreRoot) body.media_root = restoreRoot
      const r = await api.post<{ ok: boolean; restored_to: string }>(endpoint, body)
      toast(`Restored to ${r.restored_to}`)
      load()
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusy(null)
    }
  }

  if (failed) return <ErrorState title="Quarantine & Backups" noun="quarantine and backups" onRetry={load} />
  if (!quarantine || !backups) return <LoadingState title="Quarantine & Backups" noun="quarantine and backups" />

  const roots = quarantine.media_roots.length ? quarantine.media_roots : backups.media_roots
  const locationOf = (item: ArchivedItem) => {
    const parent = parentDir(item.path)
    if (roots.length === 1) return parent ? `${roots[0]}/${parent}` : roots[0]
    return parent || '·'
  }

  const sections = [
    {
      hid: 'h-q',
      title: 'Quarantine',
      count: quarantine.items.length ? `${quarantine.items.length} subtitles` : '',
      desc: "Subtitles moved out of your media folders, so players don't show them. Restore puts a subtitle back where it was.",
      emptyText: 'Nothing in quarantine.',
      items: quarantine.items,
      endpoint: '/quarantine/restore',
      isBackup: false,
    },
    {
      hid: 'h-b',
      title: 'Backups',
      count: backups.items.length ? `${backups.items.length} copies` : '',
      desc: 'Copies saved just before Verifyarr changed a subtitle.',
      emptyText: 'No backups yet. A copy is saved here each time Verifyarr changes a subtitle.',
      items: backups.items,
      endpoint: '/backups/restore',
      isBackup: true,
    },
  ]

  return (
    <div>
      {askBackup && (
        <ConfirmDialog
          title="Replace the current subtitle?"
          message={`Restoring puts this copy back at ${askBackup.original_name}. The subtitle that is there now will be replaced (a copy of it is kept as a backup), and the timing fix is undone.`}
          confirmLabel="Restore and replace"
          danger
          onConfirm={() => restore(askBackup, '/backups/restore')}
          onCancel={() => setAskBackup(null)}
        />
      )}
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17, flex: 1 }}>Quarantine &amp; Backups</h1>
        {roots.length > 1 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label htmlFor="q-root" style={{ margin: 0 }}>Restore to</label>
            <select id="q-root" value={restoreRoot} onChange={(e) => setRestoreRoot(e.target.value)} style={{ width: 'auto' }}>
              {roots.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 18 }}>
        {sections.map((sec) => (
          <section key={sec.hid} className="card" aria-labelledby={sec.hid} style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '14px 16px 10px' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                <h2 id={sec.hid} style={{ margin: 0, fontSize: 15 }}>{sec.title}</h2>
                <span className="text-dim" style={{ fontSize: 13 }}>{sec.count}</span>
              </div>
              <p className="text-dim" style={{ margin: '4px 0 0', fontSize: 13, lineHeight: 1.5 }}>{sec.desc}</p>
            </div>
            {sec.items.length === 0 && (
              <div className="text-dim" style={{ padding: '12px 16px 16px', borderTop: '1px solid var(--border)' }}>
                {sec.emptyText}
              </div>
            )}
            {sec.items.length > 0 && (
              <div role="table" aria-labelledby={sec.hid}>
                <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,2.6fr) 70px 110px 90px', gap: 12, padding: '7px 16px', borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
                  <div role="columnheader">File</div>
                  <div role="columnheader">Original location</div>
                  <div role="columnheader" style={{ textAlign: 'right' }}>Size</div>
                  <div role="columnheader">Time</div>
                  <div role="columnheader" style={{ textAlign: 'right' }}><span style={{ position: 'absolute', left: -9999 }}>Actions</span></div>
                </div>
                {sec.items.map((item) => {
                  const ts = new Date(item.mtime * 1000).toISOString()
                  const key = `${sec.endpoint}:${item.path}`
                  return (
                    <div key={item.path} role="row" data-row data-hover style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,2.6fr) 70px 110px 90px', gap: 12, alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                      <div role="cell" data-cell="main" style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 500, fontSize: 14, wordBreak: 'break-all' }}>{item.original_name}</div>
                      </div>
                      <div role="cell" data-cell data-label="Original location" className="mono" style={{ fontSize: 12, wordBreak: 'break-all', color: 'var(--text-dim)' }}>
                        {locationOf(item)}
                      </div>
                      <div role="cell" data-cell data-label="Size" style={{ textAlign: 'right' }}>{formatBytes(item.size)}</div>
                      <div role="cell" data-cell data-label="Time" style={{ color: 'var(--text-dim)' }} title={formatExact(ts)}>
                        {formatRelative(ts)}
                      </div>
                      <div role="cell" data-cell style={{ textAlign: 'right' }}>
                        <button
                          className="btn btn-sm"
                          disabled={busy === key}
                          onClick={() => (sec.isBackup ? setAskBackup(item) : restore(item, sec.endpoint))}
                          aria-label={`Restore ${item.original_name}`}
                        >
                          {busy === key ? <span className="spinner" /> : 'Restore'}
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  )
}
