import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { GeneralSettings, LibraryEntry, LibraryResponse } from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import { ErrorState, LoadingState } from '../components/PageState'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { formatExact, formatRelative } from '../lib/format'
import { runTypeLabel } from '../lib/runLabels'

interface PendingConfirm {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
}

const HEADS = [
  { key: 'title', label: 'Title', align: 'left' },
  { key: 'video_count', label: 'Videos', align: 'right' },
  { key: 'subtitle_detected_count', label: 'Subtitles', align: 'right' },
  { key: 'ok_count', label: 'Ok', align: 'right' },
  { key: 'suspect_count', label: 'Suspect', align: 'right' },
  { key: 'missing_count', label: 'Missing', align: 'right' },
  { key: 'last_processed', label: 'Last processed', align: 'left' },
] as const

type SortKey = (typeof HEADS)[number]['key']

export default function MediaLibrary({ kind, title, folderHint }: { kind: 'movie' | 'series'; title: string; folderHint: string }) {
  const [items, setItems] = useState<LibraryEntry[] | null>(null)
  const [match, setMatch] = useState<{ videos: number; matched: number } | null>(null)
  const [failed, setFailed] = useState(false)
  const [folder, setFolder] = useState('')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<SortKey>('title')
  const [dir, setDir] = useState<1 | -1>(1)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null)
  const { run: runningRun, isRunning, refresh: refreshRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()
  const noun = kind === 'movie' ? 'movies' : 'series'

  function load() {
    setFailed(false)
    api
      .get<LibraryResponse>(`/library?kind=${kind}`)
      .then((r) => { setItems(r.items); setMatch(r.bazarr_match ?? null) })
      .catch(() => {
        setItems(null)
        setFailed(true)
      })
  }

  useEffect(load, [kind])
  useEffect(() => {
    setQ('')
    setSort('title')
    setDir(1)
  }, [kind])

  useEffect(() => {
    api.get<GeneralSettings>('/settings/general').then(
      (g) => setFolder(kind === 'movie' ? g.movies_folder : g.series_folder),
      () => setFolder(''),
    )
  }, [kind])

  async function startSweep(force: boolean, itemTitle: string | undefined, busyKeyValue: string) {
    setBusyKey(busyKeyValue)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep', force, kind, title: itemTitle })
      refreshRunning()
      setBusyKey(null)
      load()
      toast(itemTitle ? `Scan started for ${itemTitle}.` : force ? 'Rescan started.' : `Scan started for all ${noun}. Only new and changed files are checked.`, {
        kind: 'info',
        action: () => navigate(`/activity/${r.run_id}`),
        actionLabel: 'View job',
      })
    } catch (err) {
      setBusyKey(null)
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    }
  }

  function rescanLibrary() {
    setConfirm({
      title: `Rescan all ${noun}?`,
      message: `This listens to every ${kind === 'movie' ? 'movie' : 'episode'} again and re-checks every subtitle, including those already in sync. On a small server it can take several hours. Verifyarr stays usable while it runs, and you can cancel it.`,
      confirmLabel: 'Rescan everything',
      onConfirm: () => startSweep(true, undefined, 'library'),
    })
  }

  if (failed) return <ErrorState title={title} noun={noun} onRetry={load} />
  if (!items) return <LoadingState title={title} noun={noun} />

  const query = q.trim().toLowerCase()
  const list = items
    .filter((t) => !query || t.title.toLowerCase().includes(query))
    .sort((a, b) => {
      const av = a[sort] ?? ''
      const bv = b[sort] ?? ''
      const cmp = typeof av === 'string' ? av.localeCompare(bv as string) : (av as number) - (bv as number)
      return cmp * dir
    })
  const videos = items.reduce((s, t) => s + t.video_count, 0)

  // A sweep with no per-title target touches this whole page; a per-title sweep shows on its row.
  const runAll = isRunning && runningRun?.mode === 'sweep' && !runningRun.target_title
    && (!runningRun.target_kind || runningRun.target_kind === kind)
    ? runningRun : null
  const rowBusy = (t: LibraryEntry) =>
    isRunning && runningRun?.mode === 'sweep'
    && (!runningRun.target_title || runningRun.target_title === t.title || runningRun.target_title.startsWith(`${t.title} `))
    && (!runningRun.target_kind || runningRun.target_kind === kind)
  const busy = busyKey !== null || isRunning

  function toggleSort(key: SortKey) {
    if (sort === key) setDir((d) => (d === 1 ? -1 : 1))
    else {
      setSort(key)
      setDir(key === 'title' ? 1 : -1)
    }
  }

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
        <h1 style={{ margin: 0, fontSize: 17 }}>{title}</h1>
        <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>
          {items.length} {kind === 'movie' ? 'movies' : 'series'} · {videos} videos
        </span>
        <input type="text" aria-label={`Search ${noun}`} placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 200 }} />
        <button className="btn" onClick={() => startSweep(false, undefined, 'library')} disabled={busy}>
          {busyKey === 'library' ? <span className="spinner" /> : 'Scan all'}
        </button>
        <button className="btn" onClick={() => rescanLibrary()} disabled={busy}>Rescan everything</button>
      </div>

      <div data-content style={{ padding: 20 }}>
        {match && match.videos > 0 && match.matched / match.videos < 0.5 && (
          <div role="alert" className="error-banner" style={{ marginBottom: 12 }}>
            Bazarr recognises only {match.matched} of {match.videos} videos. Titles and episode numbers then come from the file names. Check the path mapping under Settings → Bazarr.
          </div>
        )}
        {runAll && (
          <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', border: '1px solid #2f4a70', background: 'rgba(110,168,254,.08)', borderRadius: 'var(--radius)', marginBottom: 12, fontSize: 13 }}>
            <span className="spinner" style={{ width: 12, height: 12 }} />
            <span style={{ flex: 1 }}>
              {runTypeLabel(runAll)} is running
              {runAll.files_total ? ` (${Math.round((runAll.files_processed / runAll.files_total) * 100)}%)` : ''}.
              Counts update when files finish.
            </span>
            <button className="btn btn-sm" onClick={() => navigate(`/activity/${runAll.id}`)}>View job</button>
          </div>
        )}

        {list.length === 0 && (
          <div className="card" style={{ maxWidth: 620 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>
              {query ? `No ${noun} match “${q}”` : `No ${noun} checked yet`}
            </h2>
            <p className="text-dim" style={{ margin: '0 0 14px', lineHeight: 1.5 }}>
              {query
                ? 'Check the spelling, or clear the search to see everything.'
                : folder
                  ? `Verifyarr finds your ${noun} in ${folder}. They appear here once the first scan has checked them.`
                  : `Set the ${folderHint} folder in Settings, then scan. They appear here once the first scan has checked them.`}
            </p>
            {query
              ? <button className="btn" onClick={() => setQ('')}>Clear search</button>
              : <button className="btn btn-primary" onClick={() => startSweep(false, undefined, 'library')} disabled={busy}>Scan library</button>}
          </div>
        )}

        {list.length > 0 && (
          <div role="table" aria-label={title} className="card" style={{ padding: 0, overflowX: 'auto' }}>
            <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: 'minmax(0,2.6fr) 70px 86px 70px 80px 80px 130px 96px', minWidth: 780, gap: 12, padding: '8px 14px', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
              {HEADS.map((hd) => (
                <div key={hd.key} role="columnheader" aria-sort={sort === hd.key ? (dir > 0 ? 'ascending' : 'descending') : 'none'} style={hd.align === 'right' ? { textAlign: 'right' } : undefined}>
                  <button onClick={() => toggleSort(hd.key)} data-hover style={{ background: 'none', border: 0, padding: 0, fontWeight: 600, fontSize: 12.5, color: sort === hd.key ? 'var(--text)' : 'inherit', cursor: 'pointer' }}>
                    {hd.label}<span aria-hidden="true">{sort === hd.key ? (dir > 0 ? ' ▲' : ' ▼') : ''}</span>
                  </button>
                </div>
              ))}
              <div role="columnheader"><span style={{ position: 'absolute', left: -9999 }}>Actions</span></div>
            </div>
            {list.map((t) => {
              const busyRow = rowBusy(t)
              return (
                <div key={t.title} role="row" data-row data-hover style={{ display: 'grid', gridTemplateColumns: 'minmax(0,2.6fr) 70px 86px 70px 80px 80px 130px 96px', minWidth: 780, gap: 12, alignItems: 'center', padding: '7px 14px', borderBottom: '1px solid var(--border)' }}>
                  <div role="cell" data-cell="main" style={{ minWidth: 0 }}>
                    <button
                      onClick={() => navigate(kind === 'series' ? `/series/${encodeURIComponent(t.title)}` : `/files?title=${encodeURIComponent(t.title)}`)}
                      style={{ background: 'none', border: 0, padding: 0, color: 'var(--text)', fontWeight: 500, fontSize: 14, textAlign: 'left', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}
                    >
                      {t.title}
                    </button>
                  </div>
                  <div role="cell" data-cell data-label="Videos" style={{ textAlign: 'right' }}>{t.video_count}</div>
                  <div role="cell" data-cell data-label="Subtitles" style={{ textAlign: 'right' }}>{t.subtitle_detected_count}</div>
                  <div role="cell" data-cell data-label="Ok" className={t.ok_count ? '' : 'text-dim'} style={{ textAlign: 'right' }}>{t.ok_count}</div>
                  <div role="cell" data-cell data-label="Suspect" style={{ textAlign: 'right' }}>
                    {t.suspect_count > 0 ? (
                      <button
                        onClick={() => navigate(`/files?title=${encodeURIComponent(t.title)}&flag=attention`)}
                        title="Show suspect files"
                        style={{ background: 'none', border: 0, padding: 0, color: 'var(--red)', fontWeight: 600, fontSize: 14, cursor: 'pointer' }}
                      >
                        {t.suspect_count}
                      </button>
                    ) : (
                      <span className="text-dim">0</span>
                    )}
                  </div>
                  <div role="cell" data-cell data-label="Missing" className={t.missing_count ? '' : 'text-dim'} style={{ textAlign: 'right' }}>{t.missing_count}</div>
                  <div role="cell" data-cell data-label="Last processed" style={{ fontSize: 13, color: 'var(--text-dim)' }} title={formatExact(t.last_processed)}>
                    {formatRelative(t.last_processed)}
                  </div>
                  <div role="cell" data-cell style={{ textAlign: 'right' }}>
                    {busyRow ? (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: 'var(--blue)' }}>
                        <span className="spinner" style={{ width: 11, height: 11 }} />Scanning
                      </span>
                    ) : (
                      <button className="btn btn-sm" disabled={busy} onClick={() => startSweep(false, t.title, t.title)} aria-label={`Scan ${t.title}`}>
                        {busyKey === t.title ? <span className="spinner" /> : 'Scan'}
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
