import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { FileRow, Paginated, RunRow, StatsSummary } from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import { ErrorState, LoadingState } from '../components/PageState'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { formatExact, formatRelative } from '../lib/format'
import { languageName } from '../lib/languages'
import { runMatchesFile, runTypeLabel } from '../lib/runLabels'
import { fileDisplayName, syncText, verdict } from '../lib/verdict'

const PAGE_SIZE = 25

// The Result dropdown, mapped onto the real filter params. The current value is derived
// back from the URL by exact match, so links (dashboard, job pages) highlight correctly.
const RESULT_OPTIONS: { value: string; label: string; params: Record<string, string> }[] = [
  { value: '', label: 'All results', params: {} },
  { value: 'attention', label: 'Needs attention', params: { flag: 'attention' } },
  { value: 'insync', label: 'In sync', params: { flag: 'ok', status: 'already in sync' } },
  { value: 'fixed', label: 'Fixed', params: { sync_kind: 'fixed', flag: 'ok' } },
  { value: 'wrong_subtitle', label: 'Wrong subtitle', params: { reason: 'wrong_subtitle' } },
  { value: 'partly_out_of_sync', label: 'Partly out of sync', params: { reason: 'partly_out_of_sync' } },
  { value: 'missing_lines', label: 'Missing lines', params: { reason: 'missing_lines' } },
  { value: 'past_audio_end', label: "Doesn't fit this video", params: { reason: 'past_audio_end' } },
  { value: 'lines_out_of_order', label: 'Lines out of order', params: { reason: 'lines_out_of_order' } },
  { value: 'unreliable_timing', label: 'Unreliable timing', params: { reason: 'unreliable_timing' } },
  { value: 'repl_failed', label: 'Replacement failed', params: { flag: 'replacement_failed' } },
  { value: 'unknown', label: "Couldn't check", params: { flag: 'unknown' } },
  { value: 'other', label: 'Flagged (no reason recorded)', params: { reason: 'other' } },
  { value: 'nosub', label: 'No subtitle', params: { status: 'missing' } },
  { value: 'skipped', label: 'Skipped', params: { flag: 'skipped' } },
]

const RESULT_KEYS = ['flag', 'reason', 'status', 'sync_kind']

function resultValue(params: URLSearchParams): string {
  const cur: Record<string, string> = {}
  for (const k of RESULT_KEYS) cur[k] = params.get(k) ?? ''
  return RESULT_OPTIONS.find((o) =>
    RESULT_KEYS.every((k) => (o.params[k] ?? '') === cur[k]),
  )?.value ?? ''
}

const SORTS = [
  { key: 'video_path', label: 'File', align: 'left' },
  { key: 'lang', label: 'Language', align: 'left' },
  { key: 'sync_status', label: 'Sync', align: 'left' },
  { key: 'correctness_flag', label: 'Result', align: 'left' },
  { key: 'correctness_avg_score', label: 'Score', align: 'right' },
] as const

interface PendingConfirm {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
}

export default function Files() {
  const [params, setParams] = useSearchParams()
  const [data, setData] = useState<Paginated<FileRow> | null>(null)
  const [failed, setFailed] = useState(false)
  const [searchInput, setSearchInput] = useState(params.get('q') ?? '')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [selectAllBusy, setSelectAllBusy] = useState(false)
  const [bulkBusy, setBulkBusy] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null)
  const [sort, setSort] = useState('-last_processed')
  const [langs, setLangs] = useState<string[]>([])
  const [runsTotal, setRunsTotal] = useState<number | null>(null)
  const { run: runningRun, isRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  const q = params.get('q') ?? ''
  const flag = params.get('flag') ?? ''
  const reason = params.get('reason') ?? ''
  const status = params.get('status') ?? ''
  const lang = params.get('lang') ?? ''
  const syncKind = params.get('sync_kind') ?? ''
  const titleFilter = params.get('title') ?? ''
  const runId = params.get('run_id') ?? ''
  const which = params.get('which') ?? ''
  const page = Math.max(1, Number(params.get('page') ?? '1') || 1)

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'page') next.delete('page')
    setParams(next)
  }

  function setResult(value: string) {
    const opt = RESULT_OPTIONS.find((o) => o.value === value) ?? RESULT_OPTIONS[0]
    const next = new URLSearchParams(params)
    for (const k of RESULT_KEYS) next.delete(k)
    for (const [k, v] of Object.entries(opt.params)) next.set(k, v)
    next.delete('page')
    setParams(next)
  }

  function clearAll() {
    setParams(new URLSearchParams())
    setSearchInput('')
  }

  // Search typing is debounced into the URL; selection never survives a filter change.
  useEffect(() => {
    const t = setTimeout(() => {
      if (searchInput !== q) setParam('q', searchInput)
    }, 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput])

  const filterKey = useMemo(
    () => [q, flag, reason, status, lang, syncKind, titleFilter, runId].join('|'),
    [q, flag, reason, status, lang, syncKind, titleFilter, runId],
  )
  useEffect(() => {
    setSelected(new Set())
  }, [filterKey])

  function load() {
    setFailed(false)
    const qs = new URLSearchParams()
    if (q) qs.set('q', q)
    if (flag) qs.set('flag', flag)
    if (reason) qs.set('reason', reason)
    if (status) qs.set('status', status)
    if (lang) qs.set('lang', lang)
    if (syncKind) qs.set('sync_kind', syncKind)
    if (titleFilter) qs.set('title', titleFilter)
    if (runId) qs.set('run_id', runId)
    qs.set('sort', sort)
    qs.set('page', String(page))
    qs.set('page_size', String(PAGE_SIZE))
    api
      .get<Paginated<FileRow>>(`/files?${qs.toString()}`)
      .then(setData)
      .catch(() => {
        setData(null)
        setFailed(true)
      })
  }

  useEffect(load, [q, flag, reason, status, lang, syncKind, titleFilter, runId, sort, page])

  useEffect(() => {
    api.get<StatsSummary>('/stats/summary').then(
      (s) => setLangs(s.by_lang.map((l) => l.lang)),
      () => setLangs([]),
    )
    api.get<Paginated<RunRow>>('/runs?page_size=1').then(
      (r) => setRunsTotal(r.total),
      () => setRunsTotal(null),
    )
  }, [])

  function toggleSelected(id: number) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Selects every file matching the CURRENT filters, not just the page — refetches with a
  // page_size covering the result set (capped server-side at 5000).
  async function selectAllMatching() {
    if (!data) return
    setSelectAllBusy(true)
    try {
      const qs = new URLSearchParams()
      if (q) qs.set('q', q)
      if (flag) qs.set('flag', flag)
      if (reason) qs.set('reason', reason)
      if (status) qs.set('status', status)
      if (lang) qs.set('lang', lang)
      if (syncKind) qs.set('sync_kind', syncKind)
      if (titleFilter) qs.set('title', titleFilter)
      if (runId) qs.set('run_id', runId)
      qs.set('page', '1')
      qs.set('page_size', String(Math.min(Math.max(data.total, 1), 5000)))
      const r = await api.get<Paginated<FileRow>>(`/files?${qs.toString()}`)
      setSelected(new Set(r.items.map((f) => f.id)))
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setSelectAllBusy(false)
    }
  }

  // One request per file, sequentially, so one failure doesn't abort the rest. blacklist/
  // quarantine/remediate run synchronously server-side, so each await is enough on its own.
  async function bulkAction(action: 'blacklist' | 'quarantine' | 'remediate', ids: number[]) {
    setBulkBusy(action)
    let ok = 0
    let failedCount = 0
    for (const id of ids) {
      try {
        await api.post(`/files/${id}/${action}`)
        ok++
      } catch {
        failedCount++
      }
    }
    setSelected(new Set())
    setBulkBusy(null)
    load()
    const done = `${ok} succeeded${failedCount ? `, ${failedCount} failed` : ''}`
    if (action === 'quarantine') {
      toast(ids.length === 1 ? `Moved to quarantine. (${done})` : `Moved ${ok} subtitles to quarantine. (${done})`,
        { action: () => navigate('/quarantine'), actionLabel: 'View' })
    } else if (action === 'blacklist') {
      toast(`Blacklisted in Bazarr. (${done}) Bazarr will look for other subtitles.`,
        { action: () => navigate('/bazarr-blacklist'), actionLabel: 'View' })
    } else {
      toast(`Replacement search finished. (${done})`, { kind: failedCount ? 'warn' : 'ok' })
    }
  }

  // run-single only STARTS a background job (one runs at a time server-wide), so each file
  // is waited out before the next one starts — unlike bulkAction above.
  async function waitForRun(runIdValue: number): Promise<void> {
    for (;;) {
      const r = await api.get<RunRow>(`/runs/${runIdValue}`)
      if (r.status !== 'running') return
      await new Promise((resolve) => setTimeout(resolve, 1500))
    }
  }

  async function bulkRunNow(ids: number[]) {
    setBulkBusy('run-single')
    let ok = 0
    let failedCount = 0
    for (const id of ids) {
      try {
        const r = await api.post<{ run_id: number }>(`/files/${id}/run-single`)
        await waitForRun(r.run_id)
        ok++
      } catch {
        failedCount++
      }
    }
    setSelected(new Set())
    setBulkBusy(null)
    load()
    toast(`Re-check finished: ${ok} of ${ids.length} passed.`, { kind: failedCount ? 'warn' : 'ok' })
  }

  if (failed) return <ErrorState title="Files" noun="files" onRetry={load} />
  if (!data) return <LoadingState title="Files" noun="files" />

  const total = data.total
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const cur = Math.min(pages, page)
  const nSel = selected.size
  const pageIds = data.items.map((f) => f.id)
  const pageAll = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const selIds = [...selected]

  const one = nSel === 1 ? data.items.find((f) => f.id === selIds[0]) ?? null : null
  const what = one ? fileDisplayName(one) : `${nSel} subtitles`
  const actionsDisabled = nSel === 0 || bulkBusy !== null || isRunning
  const actionsTitle = isRunning ? 'A job is already running — wait for it to finish' : undefined

  const chip = titleFilter ? `Title: ${titleFilter}`
    : runId ? `${which === 'flagged' ? 'Flagged' : which === 'changed' ? 'Changed' : 'Touched'} by job #${runId}`
    : ''
  const anyFilter = !!(q || flag || reason || status || lang || syncKind || titleFilter || runId)

  const s0 = Math.max(1, Math.min(cur - 2, pages - 4))
  const pageNums: number[] = []
  for (let i = s0; i <= Math.min(pages, s0 + 4); i++) pageNums.push(i)

  function toggleSort(key: string) {
    setSort((s) => {
      if (s === key) return `-${key}`
      if (s === `-${key}`) return key
      return key === 'video_path' || key === 'lang' || key === 'correctness_flag' ? key : `-${key}`
    })
  }

  function togglePage() {
    setSelected((s) => {
      const next = new Set(s)
      if (pageAll) pageIds.forEach((id) => next.delete(id))
      else pageIds.forEach((id) => next.add(id))
      return next
    })
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
        <h1 style={{ margin: 0, fontSize: 17 }}>Files</h1>
        <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>
          {nSel ? `${nSel} selected` : `${total} subtitles`}
        </span>
        <button className="btn" title={actionsTitle} disabled={actionsDisabled} onClick={() => setConfirm({
          title: `Re-check ${what}?`,
          message: `Verifyarr listens to the audio again and checks ${one ? 'this subtitle' : 'each subtitle'}. On a small server this takes about a minute per file.`,
          confirmLabel: 'Re-check now',
          onConfirm: () => bulkRunNow(selIds),
        })}>
          {bulkBusy === 'run-single' ? <span className="spinner" /> : 'Re-check now'}
        </button>
        <button className="btn" title={actionsTitle} disabled={actionsDisabled} onClick={() => setConfirm({
          title: `Quarantine ${what}?`,
          message: 'They are moved out of the media folders so players stop showing them. You can restore them from Quarantine & Backups at any time.',
          confirmLabel: 'Quarantine',
          danger: true,
          onConfirm: () => bulkAction('quarantine', selIds),
        })}>
          {bulkBusy === 'quarantine' ? <span className="spinner" /> : 'Quarantine'}
        </button>
        <button className="btn" title={actionsTitle} disabled={actionsDisabled} onClick={() => setConfirm({
          title: `Blacklist ${what} in Bazarr?`,
          message: 'Bazarr will not download these again and will look for other subtitles. The files stay where they are until Bazarr replaces them.',
          confirmLabel: 'Blacklist',
          danger: true,
          onConfirm: () => bulkAction('blacklist', selIds),
        })}>
          {bulkBusy === 'blacklist' ? <span className="spinner" /> : 'Blacklist'}
        </button>
        <button className="btn btn-primary" title={actionsTitle} disabled={actionsDisabled} onClick={() => setConfirm({
          title: `Fetch replacements for ${what}?`,
          message: 'Bazarr downloads candidates and Verifyarr tests each one against the audio. If none passes, the original is kept and stays flagged. This can take a few minutes per file.',
          confirmLabel: 'Fetch replacements',
          onConfirm: () => bulkAction('remediate', selIds),
        })}>
          {bulkBusy === 'remediate' ? <span className="spinner" /> : 'Fetch replacement'}
        </button>
      </div>

      <div data-content style={{ padding: '16px 20px 24px' }}>
        <div role="search" aria-label="Filter files" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'end', marginBottom: 12 }}>
          <div style={{ width: 210 }}>
            <label htmlFor="f-res">Result</label>
            <select id="f-res" value={resultValue(params)} onChange={(e) => setResult(e.target.value)}>
              {RESULT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div style={{ width: 170 }}>
            <label htmlFor="f-sync">Sync</label>
            <select id="f-sync" value={syncKind} onChange={(e) => setParam('sync_kind', e.target.value)}>
              <option value="">Any</option>
              <option value="nochange">No change needed</option>
              <option value="moved">Moved</option>
              <option value="rescaled">Rescaled</option>
              <option value="replaced">Replaced</option>
              <option value="unchanged">Left unchanged</option>
            </select>
          </div>
          <div style={{ width: 140 }}>
            <label htmlFor="f-lang">Language</label>
            <input id="f-lang" type="text" list="f-lang-list" placeholder="e.g. en" value={lang} onChange={(e) => setParam('lang', e.target.value.trim().toLowerCase())} />
            <datalist id="f-lang-list">
              {langs.map((l) => (
                <option key={l} value={l}>{languageName(l)}</option>
              ))}
            </datalist>
          </div>
          <div style={{ flex: 1, minWidth: 180, maxWidth: 320 }}>
            <label htmlFor="f-q">Search</label>
            <input id="f-q" type="text" placeholder="Title, S01E02 or path" value={searchInput} onChange={(e) => setSearchInput(e.target.value)} />
          </div>
          {chip && (
            <span className="pill pill-info" style={{ marginBottom: 8 }}>
              {chip}
              <button
                onClick={() => {
                  const next = new URLSearchParams(params)
                  next.delete('title')
                  next.delete('run_id')
                  next.delete('which')
                  next.delete('page')
                  setParams(next)
                }}
                aria-label="Remove this filter"
                style={{ background: 'none', border: 0, color: 'inherit', padding: '0 0 0 2px', fontSize: 14, lineHeight: 1, cursor: 'pointer' }}
              >
                ×
              </button>
            </span>
          )}
          {anyFilter && (
            <button className="btn btn-sm" onClick={clearAll} style={{ marginBottom: 5 }}>Clear filters</button>
          )}
        </div>

        {isRunning && runningRun && (
          <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', border: '1px solid #2f4a70', background: 'rgba(110,168,254,.08)', borderRadius: 'var(--radius)', marginBottom: 12, fontSize: 13 }}>
            <span className="spinner" style={{ width: 12, height: 12 }} />
            <span style={{ flex: 1 }}>
              {runTypeLabel(runningRun)} is running
              {runningRun.files_total ? ` (${Math.round((runningRun.files_processed / runningRun.files_total) * 100)}%)` : ''}.
              Results update as files finish.
            </span>
            <button className="btn btn-sm" onClick={() => navigate(`/activity/${runningRun.id}`)}>View job</button>
          </div>
        )}

        {pageAll && total > pageIds.length && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: '8px 12px', background: 'var(--bg-hover)', borderRadius: 'var(--radius)', marginBottom: 10, fontSize: 13, flexWrap: 'wrap' }}>
            <span>All {pageIds.length} on this page are selected.</span>
            <button onClick={selectAllMatching} disabled={selectAllBusy} style={{ background: 'none', border: 0, color: 'var(--accent)', fontWeight: 600, padding: 0, cursor: 'pointer' }}>
              {selectAllBusy ? 'Selecting…' : total > 5000 ? 'Select first 5,000 matching' : `Select all ${total} matching`}
            </button>
          </div>
        )}

        {total === 0 && (
          <div className="card" style={{ maxWidth: 620 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>
              {anyFilter ? 'No subtitles match these filters' : runsTotal === 0 ? 'No subtitles checked yet' : 'No subtitles here yet'}
            </h2>
            <p className="text-dim" style={{ margin: '0 0 14px', lineHeight: 1.5 }}>
              {anyFilter
                ? 'Try another result or language, or clear the filters.'
                : 'Every subtitle next to your movies and episodes appears here once the first scan has checked it.'}
            </p>
            {anyFilter
              ? <button className="btn" onClick={clearAll}>Clear filters</button>
              : runsTotal === 0 && <button className="btn btn-primary" onClick={() => navigate('/')}>Scan library</button>}
          </div>
        )}

        {total > 0 && (
          <>
            <div role="table" aria-label="Subtitle files" aria-rowcount={total} className="card" style={{ padding: 0, overflowX: 'auto' }}>
              <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: '28px minmax(0,2fr) 76px minmax(0,1.1fr) minmax(0,2.6fr) 58px 78px 104px', minWidth: 900, gap: 12, alignItems: 'center', padding: '8px 14px', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
                <div role="columnheader">
                  <input type="checkbox" aria-label="Select all on this page" checked={pageAll} onChange={togglePage} style={{ width: 15, height: 15, accentColor: 'var(--accent)', margin: 0 }} />
                </div>
                {SORTS.map((hd) => {
                  const active = sort === hd.key || sort === `-${hd.key}`
                  const asc = sort === hd.key
                  return (
                    <div key={hd.key} role="columnheader" aria-sort={active ? (asc ? 'ascending' : 'descending') : 'none'} style={hd.align === 'right' ? { textAlign: 'right' } : undefined}>
                      <button
                        onClick={() => toggleSort(hd.key)}
                        data-hover
                        style={{ background: 'none', border: 0, padding: 0, fontWeight: 600, fontSize: 12.5, color: active ? 'var(--text)' : 'inherit', cursor: 'pointer' }}
                      >
                        {hd.label}<span aria-hidden="true">{active ? (asc ? ' ▲' : ' ▼') : ''}</span>
                      </button>
                    </div>
                  )
                })}
                <div role="columnheader" style={{ textAlign: 'right' }}>Swapped</div>
                <div role="columnheader" aria-sort={sort === '-last_processed' || sort === 'last_processed' ? (sort === 'last_processed' ? 'ascending' : 'descending') : 'none'}>
                  <button
                    onClick={() => toggleSort('last_processed')}
                    data-hover
                    style={{ background: 'none', border: 0, padding: 0, fontWeight: 600, fontSize: 12.5, color: sort === 'last_processed' || sort === '-last_processed' ? 'var(--text)' : 'inherit', cursor: 'pointer' }}
                  >
                    Last processed
                    <span aria-hidden="true">{sort === 'last_processed' ? ' ▲' : sort === '-last_processed' ? ' ▼' : ''}</span>
                  </button>
                </div>
              </div>
              {data.items.map((f) => {
                const v = verdict(f)
                const busy = runningRun !== null && runMatchesFile(runningRun, f)
                const busyText = runningRun?.mode === 'generate_single' ? 'Generating' : 'Checking'
                return (
                  <div key={f.id} role="row" data-row data-hover style={{ display: 'grid', gridTemplateColumns: '28px minmax(0,2fr) 76px minmax(0,1.1fr) minmax(0,2.6fr) 58px 78px 104px', minWidth: 900, gap: 12, alignItems: 'center', padding: '7px 14px', borderBottom: '1px solid var(--border)' }}>
                    <div role="cell" data-cell style={{ justifyContent: 'flex-start' }}>
                      <input type="checkbox" aria-label={`Select ${fileDisplayName(f)} ${f.lang ? languageName(f.lang) : ''}`} checked={selected.has(f.id)} onChange={() => toggleSelected(f.id)} style={{ width: 15, height: 15, accentColor: 'var(--accent)', margin: 0 }} />
                    </div>
                    <div role="cell" data-cell="main" style={{ minWidth: 0 }}>
                      <button
                        onClick={() => navigate(`/files/${f.id}`)}
                        style={{ background: 'none', border: 0, padding: 0, color: 'var(--text)', fontWeight: 500, textAlign: 'left', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block', cursor: 'pointer', fontSize: 14 }}
                      >
                        {fileDisplayName(f)}
                      </button>
                    </div>
                    <div role="cell" data-cell data-label="Language">{f.lang ? languageName(f.lang) : '—'}</div>
                    <div role="cell" data-cell data-label="Sync" className={v.key === 'fixed_shift' || v.key === 'fixed_rate' || v.key === 'insync' && syncText(f) === 'Replaced' ? '' : 'text-dim'} style={{ fontSize: 13 }}>
                      {syncText(f)}
                    </div>
                    <div role="cell" data-cell data-label="Result" style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
                      {busy ? (
                        <span className="pill pill-info"><span className="spinner" style={{ width: 10, height: 10 }} />{busyText}</span>
                      ) : (
                        <span className={`pill ${v.cls}`}><span aria-hidden="true">{v.icon}</span>{v.label}</span>
                      )}
                      <span className="text-dim" style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }} title={v.happened}>
                        {v.happened}
                      </span>
                    </div>
                    <div role="cell" data-cell data-label="Score" className={f.correctness_avg_score === null ? 'text-dim' : ''} style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {f.correctness_avg_score === null ? '—' : f.correctness_avg_score.toFixed(2)}
                    </div>
                    <div role="cell" data-cell data-label="Swapped lines" style={{ textAlign: 'right', fontSize: 13 }}>
                      {(f.line_order_flagged ?? 0) > 0 ? (
                        // Red only when the swaps condemned the file; otherwise a few uncertain pairs.
                        f.reason === 'lines_out_of_order'
                          ? <span style={{ color: 'var(--red)' }}>{f.line_order_flagged} flagged</span>
                          : <span className="text-dim" title="Possibly swapped, not changed: too uncertain to fix">{f.line_order_flagged} unsure</span>
                      ) : (f.line_order_fixed ?? 0) > 0 ? (
                        <span>{f.line_order_fixed} fixed</span>
                      ) : (
                        <span className="text-dim">—</span>
                      )}
                    </div>
                    <div role="cell" data-cell data-label="Last processed" style={{ fontSize: 13, color: 'var(--text-dim)' }} title={formatExact(f.last_processed)}>
                      {formatRelative(f.last_processed)}
                    </div>
                  </div>
                )
              })}
            </div>
            <nav aria-label="Pages" style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 12, flexWrap: 'wrap' }}>
              <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>
                Showing {(cur - 1) * PAGE_SIZE + 1}–{Math.min(total, cur * PAGE_SIZE)} of {total}
              </span>
              <button className="btn btn-sm" onClick={() => setParam('page', String(cur - 1))} disabled={cur <= 1}>‹ Previous</button>
              {pageNums.map((n) => (
                <button
                  key={n}
                  className={`btn btn-sm ${n === cur ? 'btn-primary' : ''}`}
                  onClick={() => setParam('page', String(n))}
                  aria-current={n === cur ? 'page' : undefined}
                  style={{ minWidth: 32, justifyContent: 'center' }}
                >
                  {n}
                </button>
              ))}
              <button className="btn btn-sm" onClick={() => setParam('page', String(cur + 1))} disabled={cur >= pages}>Next ›</button>
            </nav>
          </>
        )}
      </div>
    </div>
  )
}
