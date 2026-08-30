import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { FileRow, Paginated, RunRow } from '../api/types'
import { fileName, formatRelative } from '../lib/format'
import StatusPill from '../components/StatusPill'
import styles from './Files.module.css'

const PAGE_SIZE = 30

// Line-order check (see Settings -> Automation) — null/null means it wasn't checked (feature off,
// or file never processed since it was turned on).
function LineOrderCell({ f }: { f: FileRow }) {
  if (f.line_order_flagged == null && f.line_order_fixed == null) {
    return <span className="pill pill-muted">—</span>
  }
  if ((f.line_order_flagged ?? 0) > 0) {
    return <span className="pill pill-warn">{f.line_order_flagged} flagged</span>
  }
  if ((f.line_order_fixed ?? 0) > 0) {
    return <span className="pill pill-ok">{f.line_order_fixed} fixed</span>
  }
  return <span className="pill pill-ok">ok</span>
}

export default function Files() {
  const [params, setParams] = useSearchParams()
  const [data, setData] = useState<Paginated<FileRow> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [searchInput, setSearchInput] = useState(params.get('q') ?? '')
  const [busyId, setBusyId] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkBusy, setBulkBusy] = useState<string | null>(null)
  const [bulkResult, setBulkResult] = useState<string | null>(null)
  const [selectAllBusy, setSelectAllBusy] = useState(false)

  const q = params.get('q') ?? ''
  const flag = params.get('flag') ?? ''
  const status = params.get('status') ?? ''
  const lang = params.get('lang') ?? ''
  const page = Number(params.get('page') ?? '1')

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'page') next.delete('page')
    setParams(next)
  }

  useEffect(() => {
    const t = setTimeout(() => {
      if (searchInput !== q) setParam('q', searchInput)
    }, 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput])

  function load() {
    setLoading(true)
    const qs = new URLSearchParams()
    if (q) qs.set('q', q)
    if (flag) qs.set('flag', flag)
    if (status) qs.set('status', status)
    if (lang) qs.set('lang', lang)
    qs.set('page', String(page))
    qs.set('page_size', String(PAGE_SIZE))
    api
      .get<Paginated<FileRow>>(`/files?${qs.toString()}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [q, flag, status, lang, page])

  async function runSingle(id: number) {
    setBusyId(id)
    setError(null)
    try {
      await api.post(`/files/${id}/run-single`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  async function remediate(id: number) {
    setBusyId(id)
    setError(null)
    try {
      await api.post(`/files/${id}/remediate`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  function toggleSelected(id: number) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Selects every file matching the CURRENT search/filters, not just the current page — refetches
  // with a page_size covering the whole result set (capped server-side, see routers/files.py).
  async function toggleSelectAll() {
    if (!data) return
    if (selected.size > 0) {
      setSelected(new Set())
      return
    }
    setSelectAllBusy(true)
    setError(null)
    try {
      const qs = new URLSearchParams()
      if (q) qs.set('q', q)
      if (flag) qs.set('flag', flag)
      if (status) qs.set('status', status)
      if (lang) qs.set('lang', lang)
      qs.set('page', '1')
      qs.set('page_size', String(Math.min(Math.max(data.total, 1), 5000)))
      const r = await api.get<Paginated<FileRow>>(`/files?${qs.toString()}`)
      setSelected(new Set(r.items.map((f) => f.id)))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSelectAllBusy(false)
    }
  }

  // One request per file, sequentially, so one failure doesn't abort the rest. blacklist/
  // quarantine/remediate all run synchronously server-side (see routers/files.py's _apply_action)
  // so each await is enough on its own.
  async function bulkAction(action: 'blacklist' | 'quarantine' | 'remediate') {
    setBulkBusy(action)
    setBulkResult(null)
    let ok = 0
    let failed = 0
    for (const id of selected) {
      try {
        await api.post(`/files/${id}/${action}`)
        ok++
      } catch {
        failed++
      }
    }
    setBulkResult(`${action}: ${ok} succeeded${failed ? `, ${failed} failed` : ''}.`)
    setSelected(new Set())
    setBulkBusy(null)
    load()
  }

  // run-single is different: it only STARTS a background job (only one can run at a time server-
  // wide, see jobs.JobRunner), so each file has to be waited out before starting the next one —
  // unlike bulkAction above, which posts to synchronous endpoints.
  async function waitForRun(runId: number): Promise<void> {
    for (;;) {
      const r = await api.get<RunRow>(`/runs/${runId}`)
      if (r.status !== 'running') return
      await new Promise((resolve) => setTimeout(resolve, 1500))
    }
  }

  async function bulkRunNow() {
    setBulkBusy('run-single')
    setBulkResult(null)
    let ok = 0
    let failed = 0
    for (const id of selected) {
      try {
        const r = await api.post<{ run_id: number }>(`/files/${id}/run-single`)
        await waitForRun(r.run_id)
        ok++
      } catch {
        failed++
      }
    }
    setBulkResult(`run now: ${ok} succeeded${failed ? `, ${failed} failed` : ''}.`)
    setSelected(new Set())
    setBulkBusy(null)
    load()
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <div>
      <h1>Files</h1>
      {error && <div className="error-banner">{error}</div>}

      <div className={styles.toolbar}>
        <input
          className={styles.search}
          type="text"
          placeholder="Search title or filename…"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
        />
        <select value={flag} onChange={(e) => setParam('flag', e.target.value)}>
          <option value="">All flags</option>
          <option value="ok">ok</option>
          <option value="SUSPECT">SUSPECT</option>
          <option value="skipped">skipped</option>
        </select>
        <select value={status} onChange={(e) => setParam('status', e.target.value)}>
          <option value="">All statuses</option>
          <option value="missing">missing</option>
          <option value="already in sync">already in sync</option>
        </select>
        <select value={lang} onChange={(e) => setParam('lang', e.target.value)}>
          <option value="">All languages</option>
          <option value="da">da</option>
          <option value="en">en</option>
        </select>
      </div>

      {selected.size > 0 && (
        <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, padding: '10px 14px' }}>
          <span style={{ fontSize: 13 }}>{selected.size} selected</span>
          <button className="btn btn-sm" disabled={!!bulkBusy} onClick={bulkRunNow}>
            {bulkBusy === 'run-single' ? <span className="spinner" /> : 'Run now selected'}
          </button>
          <button className="btn btn-sm" disabled={!!bulkBusy} onClick={() => bulkAction('quarantine')}>
            {bulkBusy === 'quarantine' ? <span className="spinner" /> : 'Quarantine selected'}
          </button>
          <button className="btn btn-sm" disabled={!!bulkBusy} onClick={() => bulkAction('blacklist')}>
            {bulkBusy === 'blacklist' ? <span className="spinner" /> : 'Blacklist selected'}
          </button>
          <button className="btn btn-sm btn-danger" disabled={!!bulkBusy} onClick={() => bulkAction('remediate')}>
            {bulkBusy === 'remediate' ? <span className="spinner" /> : 'Remediate selected'}
          </button>
          <button className="btn btn-sm" disabled={!!bulkBusy} onClick={() => setSelected(new Set())}>
            Clear
          </button>
        </div>
      )}
      {bulkResult && <div className="card" style={{ marginBottom: 14, fontSize: 13, padding: '10px 14px' }}>{bulkResult}</div>}

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th style={{ width: 28 }}>
                {selectAllBusy ? (
                  <span className="spinner" />
                ) : (
                  <input
                    type="checkbox"
                    checked={selected.size > 0}
                    ref={(el) => {
                      if (el) el.indeterminate = selected.size > 0 && data ? selected.size < data.total : false
                    }}
                    onChange={toggleSelectAll}
                  />
                )}
              </th>
              <th>Episode / file</th>
              <th>Language</th>
              <th>Sync</th>
              <th>Correctness</th>
              <th>Score</th>
              <th>Line order</th>
              <th>Last processed</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={9} className="text-dim">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && data?.items.length === 0 && (
              <tr>
                <td colSpan={9} className="text-dim">
                  No files match.
                </td>
              </tr>
            )}
            {data?.items.map((f) => (
              <tr key={f.id}>
                <td>
                  <input type="checkbox" checked={selected.has(f.id)} onChange={() => toggleSelected(f.id)} />
                </td>
                <td>
                  <Link className={styles.rowLink} to={`/files/${f.id}`}>
                    <div>{f.season_episode ?? fileName(f.video_path)}</div>
                    <div className="text-faint" style={{ fontSize: 11.5 }}>
                      {f.series_or_movie_title ?? fileName(f.video_path)}
                    </div>
                  </Link>
                </td>
                <td>{f.lang ?? '—'}</td>
                <td>
                  <StatusPill value={f.sync_status} />
                </td>
                <td>
                  <StatusPill value={f.correctness_flag} />
                </td>
                <td className="mono">{f.correctness_avg_score ?? '—'}</td>
                <td>
                  <LineOrderCell f={f} />
                </td>
                <td className="text-dim">{formatRelative(f.last_processed)}</td>
                <td>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {f.subtitle_path && (
                      <button
                        className="btn btn-sm"
                        disabled={busyId === f.id}
                        onClick={() => runSingle(f.id)}
                      >
                        Run now
                      </button>
                    )}
                    {f.correctness_flag === 'SUSPECT' && (
                      <button
                        className="btn btn-sm btn-danger"
                        disabled={busyId === f.id}
                        onClick={() => remediate(f.id)}
                      >
                        Remediate
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className={styles.pager}>
        <span>{data ? `${data.total} files` : ''}</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn btn-sm" disabled={page <= 1} onClick={() => setParam('page', String(page - 1))}>
            ← Previous
          </button>
          <span>
            Page {page} of {totalPages}
          </span>
          <button
            className="btn btn-sm"
            disabled={page >= totalPages}
            onClick={() => setParam('page', String(page + 1))}
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  )
}
