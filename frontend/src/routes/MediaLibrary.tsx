import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryResponse, SeasonEntry } from '../api/types'
import { formatRelative } from '../lib/format'
import { useRunningJob } from '../hooks/useRunningJob'
import ConfirmDialog from '../components/ConfirmDialog'

interface PendingConfirm {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
}

const CELL = { padding: '9px 12px' }

export default function MediaLibrary({ kind, title, folderHint }: { kind: 'movie' | 'series'; title: string; folderHint: string }) {
  const [items, setItems] = useState<LibraryEntry[] | null>(null)
  const [lastScannedAt, setLastScannedAt] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  // 'library' = the whole-page Scan/Rescan button, a title for a per-show Scan, or
  // "<title>::<season>" for a per-season Scan.
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const { isRunning, refresh: refreshRunning } = useRunningJob()
  const navigate = useNavigate()

  function load() {
    setItems(null)
    api
      .get<LibraryResponse>(`/library?kind=${kind}`)
      .then((r) => {
        setItems(r.items)
        setLastScannedAt(r.last_scanned_at)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  useEffect(load, [kind])

  function toggleExpanded(t: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  async function startSweep(force: boolean, itemTitle: string | undefined, season: string | undefined, busyKeyValue: string) {
    setBusyKey(busyKeyValue)
    setError(null)
    try {
      const r = await api.post<{ run_id: number }>('/runs', {
        mode: 'sweep',
        force,
        kind,
        title: itemTitle,
        season,
      })
      refreshRunning()
      navigate(`/activity/${r.run_id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusyKey(null)
    }
  }

  function scanLibrary() {
    const alreadyScanned = items?.some((it) => it.processed_count > 0) ?? false
    if (alreadyScanned) {
      setConfirm({
        title: 'Already scanned',
        message: `${title} has already been scanned before. Scanning again will only process files that are new or have changed since — already-processed files are left alone unless something about them changed.`,
        confirmLabel: 'Scan again',
        onConfirm: () => {
          setConfirm(null)
          startSweep(false, undefined, undefined, 'library')
        },
      })
      return
    }
    startSweep(false, undefined, undefined, 'library')
  }

  function rescanLibrary() {
    setConfirm({
      title: 'Rescan everything?',
      message: `This forces every ${kind} file through again, including ones already scanned — not just new or changed ones — using whatever's turned on under Settings → Automation → What runs. Can take a while depending on how much you have, and (if correctness or line-order checking is on) use an API call per file.`,
      confirmLabel: 'Rescan all',
      danger: true,
      onConfirm: () => {
        setConfirm(null)
        startSweep(true, undefined, undefined, 'library')
      },
    })
  }

  function scanOne(it: LibraryEntry) {
    if (it.processed_count > 0) {
      setConfirm({
        title: 'Already scanned',
        message: `"${it.title}" has already been scanned before. Scanning again will only process files that are new or have changed since.`,
        confirmLabel: 'Scan again',
        onConfirm: () => {
          setConfirm(null)
          startSweep(false, it.title, undefined, it.title)
        },
      })
      return
    }
    startSweep(false, it.title, undefined, it.title)
  }

  function scanSeason(it: LibraryEntry, s: SeasonEntry) {
    const key = `${it.title}::${s.season}`
    if (s.processed_count > 0) {
      setConfirm({
        title: 'Already scanned',
        message: `"${it.title}" ${s.season} has already been scanned before. Scanning again will only process files that are new or have changed since.`,
        confirmLabel: 'Scan again',
        onConfirm: () => {
          setConfirm(null)
          startSweep(false, it.title, s.season, key)
        },
      })
      return
    }
    startSweep(false, it.title, s.season, key)
  }

  const filtered = items?.filter((it) => it.title.toLowerCase().includes(q.toLowerCase()))
  const busy = busyKey !== null || isRunning

  return (
    <div>
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          confirmLabel={confirm.confirmLabel}
          danger={confirm.danger}
          onConfirm={confirm.onConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h1 style={{ marginBottom: 4 }}>{title}</h1>
          <p className="text-dim" style={{ maxWidth: 640, marginBottom: 16 }}>
            Everything found under your {folderHint} folder, processed or not. Scan checks files
            that are new or changed, using whatever's turned on under Settings → Automation →
            What runs. Rescan forces every file through again, even ones already done.
            {kind === 'series' && ' Expand a show to scan a single season.'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" disabled={busy} onClick={scanLibrary}>
            {busyKey === 'library' ? <span className="spinner" /> : '▶ Scan'}
          </button>
          <button className="btn" disabled={busy} onClick={rescanLibrary}>
            ⟳ Rescan
          </button>
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {isRunning && busyKey === null && (
        <div className="card" style={{ marginBottom: 14, fontSize: 13, padding: '10px 14px' }}>
          A job is already running — wait for it to finish before starting a scan.
        </div>
      )}

      <div style={{ marginBottom: 16, display: 'flex', gap: 16, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="Search title…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ maxWidth: 280 }}
        />
        <span className="text-faint" style={{ fontSize: 12 }}>
          {lastScannedAt ? `Last scanned ${formatRelative(lastScannedAt)}` : 'Never scanned yet'}
        </span>
      </div>

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: 'left', ...CELL }}>Title</th>
              <th style={{ textAlign: 'left', ...CELL }}>Videos</th>
              <th style={{ textAlign: 'left', ...CELL }}>Subtitles detected</th>
              <th style={{ textAlign: 'left', ...CELL }}>Processed</th>
              <th style={{ textAlign: 'left', ...CELL }}>Ok / Suspect / Missing</th>
              <th style={{ textAlign: 'left', ...CELL }}>Last processed</th>
              <th style={{ textAlign: 'left', ...CELL }}></th>
            </tr>
          </thead>
          <tbody>
            {!items && (
              <tr>
                <td colSpan={7} style={CELL} className="text-dim">
                  <span className="spinner" /> Loading…
                </td>
              </tr>
            )}
            {items && items.length === 0 && (
              <tr>
                <td colSpan={7} style={CELL} className="text-dim">
                  Nothing found yet — check the {folderHint} folder under Settings → General, then
                  click Scan.
                </td>
              </tr>
            )}
            {items && items.length > 0 && filtered?.length === 0 && (
              <tr>
                <td colSpan={7} style={CELL} className="text-dim">
                  No titles match "{q}".
                </td>
              </tr>
            )}
            {filtered?.map((it) => {
              const hasSeasons = kind === 'series' && !!it.seasons && it.seasons.length > 0
              const isOpen = expanded.has(it.title)
              return (
                <>
                  <tr key={it.title} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={CELL}>
                      {hasSeasons && (
                        <button
                          onClick={() => toggleExpanded(it.title)}
                          aria-label={isOpen ? 'Collapse' : 'Expand'}
                          style={{
                            background: 'none', border: 'none', color: 'var(--text-dim)', cursor: 'pointer',
                            marginRight: 6, padding: 0, fontSize: 11, width: 14, display: 'inline-block',
                          }}
                        >
                          {isOpen ? '▾' : '▸'}
                        </button>
                      )}
                      <Link to={`/files?q=${encodeURIComponent(it.title)}`}>{it.title}</Link>
                    </td>
                    <td style={CELL}>{it.video_count}</td>
                    <td style={CELL}>
                      {it.subtitle_detected_count} / {it.video_count}
                      {it.subtitle_detected_count < it.video_count && (
                        <span className="pill pill-warn" style={{ marginLeft: 6 }}>
                          missing
                        </span>
                      )}
                    </td>
                    <td style={CELL}>
                      {it.processed_count} / {it.video_count}
                      {it.processed_count === 0 && (
                        <span className="pill pill-muted" style={{ marginLeft: 6 }}>
                          never run
                        </span>
                      )}
                    </td>
                    <td style={CELL}>
                      <span style={{ color: 'var(--green)' }}>{it.ok_count}</span>
                      {' / '}
                      <span style={{ color: 'var(--red)' }}>{it.suspect_count}</span>
                      {' / '}
                      <span style={{ color: 'var(--yellow)' }}>{it.missing_count}</span>
                    </td>
                    <td style={CELL} className="text-dim">
                      {formatRelative(it.last_processed)}
                    </td>
                    <td style={CELL}>
                      <button className="btn btn-sm" disabled={busy} onClick={() => scanOne(it)}>
                        {busyKey === it.title ? <span className="spinner" /> : 'Scan'}
                      </button>
                    </td>
                  </tr>
                  {hasSeasons &&
                    isOpen &&
                    it.seasons!.map((s) => {
                      const key = `${it.title}::${s.season}`
                      return (
                        <tr key={key} style={{ borderTop: '1px solid var(--border)', background: 'var(--bg)' }}>
                          <td style={{ ...CELL, paddingLeft: 34 }} className="text-dim">
                            {s.season}
                          </td>
                          <td style={CELL}>{s.video_count}</td>
                          <td style={CELL}>
                            {s.subtitle_detected_count} / {s.video_count}
                            {s.subtitle_detected_count < s.video_count && (
                              <span className="pill pill-warn" style={{ marginLeft: 6 }}>
                                missing
                              </span>
                            )}
                          </td>
                          <td style={CELL}>
                            {s.processed_count} / {s.video_count}
                            {s.processed_count === 0 && (
                              <span className="pill pill-muted" style={{ marginLeft: 6 }}>
                                never run
                              </span>
                            )}
                          </td>
                          <td style={CELL}>
                            <span style={{ color: 'var(--green)' }}>{s.ok_count}</span>
                            {' / '}
                            <span style={{ color: 'var(--red)' }}>{s.suspect_count}</span>
                            {' / '}
                            <span style={{ color: 'var(--yellow)' }}>{s.missing_count}</span>
                          </td>
                          <td style={CELL} className="text-dim">
                            {formatRelative(s.last_processed)}
                          </td>
                          <td style={CELL}>
                            <button className="btn btn-sm" disabled={busy} onClick={() => scanSeason(it, s)}>
                              {busyKey === key ? <span className="spinner" /> : 'Scan'}
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                </>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
