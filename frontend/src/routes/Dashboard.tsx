import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { AttentionResponse, FileRow, NextRunResponse, Paginated, RunRow, StatsSummary } from '../api/types'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { runPct } from '../lib/progress'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { formatExact, formatRelative, nextScanLabel } from '../lib/format'
import { runTargetLabel, runTypeLabel } from '../lib/runLabels'
import { ATTENTION_META, changeLines, fileDisplayName, syncText } from '../lib/verdict'
import { languageName } from '../lib/languages'

interface DashboardData {
  summary: StatsSummary
  attention: AttentionResponse
  runs: Paginated<RunRow> & { current_run_id: number | null }
  running: RunRow | null
  nextRunAt: string | null
  recent: FileRow[]
}

function Grid2({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,340px),1fr))', gap: 16 }}>
      {children}
    </div>
  )
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [failed, setFailed] = useState(false)
  const [starting, setStarting] = useState(false)
  const { refresh: refreshRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  const load = useCallback(async () => {
    setFailed(false)
    try {
      const [summary, attention, runs, next, recent] = await Promise.all([
        api.get<StatsSummary>('/stats/summary'),
        api.get<AttentionResponse>('/stats/attention'),
        api.get<Paginated<RunRow> & { current_run_id: number | null }>('/runs?page_size=6'),
        api.get<NextRunResponse>('/runs/next'),
        api.get<Paginated<FileRow>>('/files?sync_kind=fixed&flag=ok&page_size=10&sort=-last_processed'),
      ])
      const running = runs.current_run_id !== null
        ? await api.get<RunRow>(`/runs/${runs.current_run_id}`).catch(() => null)
        : null
      setData({ summary, attention, runs, running, nextRunAt: next.next_run_at, recent: recent.items })
    } catch {
      setData(null)
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // While a job runs, the numbers move — refresh behind it on the same cadence as the sidebar.
  useEffect(() => {
    const id = setInterval(load, 8000)
    return () => clearInterval(id)
  }, [load])

  async function scan() {
    setStarting(true)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep' })
      refreshRunning()
      load()
      toast('Scan started.', { kind: 'info', action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job' })
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), { kind: 'bad' })
    } finally {
      setStarting(false)
    }
  }

  if (failed) return <ErrorState title="Dashboard" noun="the dashboard" onRetry={load} />
  if (!data) return <LoadingState title="Dashboard" noun="the dashboard" />

  const { summary, attention, runs, running, recent } = data
  const h = summary.health
  const n = (v: number | null) => v ?? 0
  const scanned = summary.files.total > 0
  const firstRun = !scanned && running

  const toolbar = (
    <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
      <h1 style={{ margin: 0, fontSize: 17, flex: 1 }}>Dashboard</h1>
      <button className="btn" onClick={() => navigate('/activity')}>Activity</button>
      <button className="btn btn-primary" onClick={scan} disabled={!!running || starting}>
        {running ? 'Scan running…' : starting ? <span className="spinner" /> : 'Scan library'}
      </button>
    </div>
  )

  if (!scanned) {
    const pct = running && running.files_total
      ? runPct(running)
      : null
    return (
      <>
        {toolbar}
        <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card" style={{ maxWidth: 680, padding: 22 }}>
            <h2 style={{ fontSize: 17, margin: '0 0 8px' }}>
              {firstRun ? 'First scan running' : runs.total > 0 ? 'No subtitles found' : 'No scan yet'}
            </h2>
            <p className="text-dim" style={{ margin: '0 0 16px', lineHeight: 1.55 }}>
              {firstRun
                ? 'Verifyarr is listening to each movie and episode and checking every subtitle against it. The dashboard fills in when the scan finishes. You can close this page; the scan keeps running.'
                : runs.total > 0
                  ? 'The scan is done but found no video files to check. Make sure the media folders in Settings point at your movies and series, then scan again.'
                  : "Verifyarr hasn't checked your library yet. Press Scan library when you're ready; the first scan listens to each movie and episode and checks every subtitle against it. On a small server this can take a few hours; you can keep using Verifyarr meanwhile."}
            </p>
            {firstRun && running && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                  <span>{running.files_total ? `${running.files_processed} of ${running.files_total} subtitle files checked` : 'Finding files to check…'}</span>
                  {pct !== null && <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{pct}%</span>}
                </div>
                {pct !== null && (
                  <div role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="First scan progress" style={{ height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
                    <svg aria-hidden="true" width="100%" height="8" style={{ display: 'block' }}>
                      <rect x="0" y="0" width={`${pct}%`} height="8" style={{ fill: 'var(--accent)' }} />
                    </svg>
                  </div>
                )}
                <div className="text-dim" style={{ fontSize: 13 }}>
                  So far: {running.files_changed} fixed · {running.files_suspect} need attention
                </div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              {firstRun && running
                ? <button className="btn" onClick={() => navigate(`/activity/${running.id}`)}>Watch the scan</button>
                : <button className="btn btn-primary" onClick={scan} disabled={starting}>Scan library</button>}
            </div>
          </div>
        </div>
      </>
    )
  }

  const checked = summary.files.total - n(h.missing) - n(h.skipped)
  const good = n(h.insync) + n(h.fixed) + n(h.generated)
  const pct = checked ? Math.round((good / checked) * 1000) / 10 : 0
  const seg = (v: number) => (checked ? (v / checked) * 100 : 0)
  const w1 = seg(n(h.insync))
  const w2 = seg(n(h.fixed) + n(h.generated))
  const w3 = seg(n(h.suspect))
  const w4 = seg(n(h.unknown))
  const w5 = seg(n(h.other))

  const attRows = attention.items.map((i) => ({ ...ATTENTION_META[i.reason], count: i.count, to: `/files?reason=${i.reason}` }))
  if (n(h.missing) > 0 || n(summary.files.missing) > 0) {
    attRows.push({ ...ATTENTION_META.nosub, count: n(h.missing), to: '/files?status=missing' })
  }
  const attTotal = attRows.reduce((s, r) => s + r.count, 0)

  const job: RunRow | null = running ?? runs.items[0] ?? summary.last_run ?? null
  const jobRunning = job?.status === 'running'
  const jobPct = job ? runPct(job) : null

  return (
    <>
      {toolbar}
      <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,270px),1fr))', gap: 16 }}>
          <section className="card" aria-labelledby="h-health">
            <h2 id="h-health" style={{ fontSize: 12.5, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 0.5, margin: '0 0 10px' }}>
              Library health
            </h2>
            {checked === 0 ? (
              <div className="text-dim" style={{ fontSize: 13 }}>No subtitles checked yet.</div>
            ) : (
              <>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span style={{ fontSize: 34, fontWeight: 700, lineHeight: 1 }}>{pct}%</span>
                  <span className="text-dim">of subtitles in sync</span>
                </div>
                <div className="text-dim" style={{ fontSize: 12.5, margin: '6px 0 12px' }}>
                  {checked} subtitles checked in {n(h.videos)} videos
                </div>
                <div aria-hidden="true" style={{ height: 8, borderRadius: 4, overflow: 'hidden', background: 'var(--border)' }}>
                  <svg width="100%" height="8" style={{ display: 'block' }}>
                    <rect x="0" y="0" width={`${w1}%`} height="8" style={{ fill: 'var(--green)' }} />
                    <rect x={`${w1}%`} y="0" width={`${w2}%`} height="8" style={{ fill: 'var(--accent-dim)' }} />
                    <rect x={`${w1 + w2}%`} y="0" width={`${w3}%`} height="8" style={{ fill: 'var(--red)' }} />
                    <rect x={`${w1 + w2 + w3}%`} y="0" width={`${w4}%`} height="8" style={{ fill: 'var(--grey)' }} />
                    {w5 > 0 && <rect x={`${w1 + w2 + w3 + w4}%`} y="0" width={`${w5}%`} height="8" style={{ fill: 'var(--yellow)' }} />}
                  </svg>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 12px', marginTop: 10, fontSize: 12.5 }}>
                  {[
                    { color: 'var(--green)', label: 'In sync', v: n(h.insync) },
                    { color: 'var(--accent-dim)', label: 'Fixed', v: n(h.fixed) + n(h.generated) },
                    { color: 'var(--red)', label: 'Flagged', v: n(h.suspect) },
                    { color: 'var(--grey)', label: "Couldn't check", v: n(h.unknown) },
                    ...(n(h.other) > 0 ? [{ color: 'var(--yellow)', label: 'Other', v: n(h.other) }] : []),
                  ].map((l) => (
                    <div key={l.label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: 2, background: l.color }} />
                      <span className="text-dim" style={{ flex: 1 }}>{l.label}</span>
                      <span style={{ fontWeight: 600 }}>{l.v}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </section>

          <section className="card" aria-labelledby="h-job" style={{ display: 'flex', flexDirection: 'column' }}>
            <h2 id="h-job" style={{ fontSize: 12.5, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 0.5, margin: '0 0 10px' }}>
              {job ? (jobRunning ? 'Running now' : 'Last job') : 'Last job'}
            </h2>
            {job ? (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <span style={{ fontWeight: 600, fontSize: 15 }}>{runTypeLabel(job)}</span>
                  <StatusPill value={job.status} />
                </div>
                <div className="text-dim" style={{ fontSize: 13, marginBottom: 10 }}>
                  {runTargetLabel(job)} ·{' '}
                  <span title={formatExact(jobRunning ? job.started_at : (job.finished_at ?? job.started_at))}>
                    {jobRunning ? `started ${formatRelative(job.started_at)}` : `finished ${formatRelative(job.finished_at ?? job.started_at)}`}
                  </span>
                </div>
                {jobRunning && jobPct !== null && (
                  <div role="progressbar" aria-label="Job progress" aria-valuenow={jobPct} aria-valuemin={0} aria-valuemax={100} style={{ height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden', marginBottom: 8 }}>
                    <svg aria-hidden="true" width="100%" height="6" style={{ display: 'block' }}>
                      <rect x="0" y="0" width={`${jobPct}%`} height="6" style={{ fill: 'var(--accent)' }} />
                    </svg>
                  </div>
                )}
                <div style={{ fontSize: 13 }}>
                  {jobRunning
                    ? `${job.files_total ? `${job.files_processed} of ${job.files_total} files` : 'Finding files…'} · ${job.files_changed} changed · ${job.files_suspect} flagged`
                    : `${job.files_processed} files · ${job.files_changed} changed · ${job.files_suspect} flagged · ${job.files_error} errors`}
                </div>
                <div style={{ marginTop: 'auto', paddingTop: 12 }}>
                  <button className="btn btn-sm" onClick={() => navigate(`/activity/${job.id}`)}>Open job</button>
                </div>
              </>
            ) : (
              <div className="text-dim" style={{ fontSize: 13 }}>No jobs yet.</div>
            )}
          </section>

          <section className="card" aria-labelledby="h-next" style={{ display: 'flex', flexDirection: 'column' }}>
            <h2 id="h-next" style={{ fontSize: 12.5, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 0.5, margin: '0 0 10px' }}>
              Next scheduled scan
            </h2>
            <div style={{ fontSize: 20, fontWeight: 600 }} title={data.nextRunAt ? formatExact(data.nextRunAt) : ''}>
              {nextScanLabel(data.nextRunAt)}
            </div>
            <div className="text-dim" style={{ fontSize: 13, marginTop: 4, lineHeight: 1.5 }}>
              {data.nextRunAt
                ? `${formatRelative(data.nextRunAt).replace(/^in /, 'In ')}. Checks files that are new or changed since the last scan.`
                : 'The schedule is empty or invalid.'}
            </div>
            <div style={{ marginTop: 'auto', paddingTop: 12 }}>
              <button className="btn btn-sm" onClick={() => navigate('/settings/scheduling')}>Change schedule</button>
            </div>
          </section>
        </div>

        <Grid2>
          <section className="card" aria-labelledby="h-att" style={{ padding: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 16px 10px' }}>
              <h2 id="h-att" style={{ fontSize: 15, margin: 0, flex: 1 }}>Needs attention</h2>
              <span className="text-dim" style={{ fontSize: 13 }}>{attTotal} subtitles</span>
            </div>
            {attRows.length === 0 && (
              <div style={{ padding: '6px 16px 18px', color: 'var(--text-dim)' }}>
                <span aria-hidden="true" style={{ color: 'var(--green)' }}>✓ </span>
                Nothing needs attention. Every checked subtitle is in sync.
              </div>
            )}
            {attRows.map((t) => (
              <button
                key={t.label}
                onClick={() => navigate(t.to)}
                aria-label={`${t.count} ${t.label}: ${t.todo} Show these files`}
                data-hover
                style={{ display: 'grid', gridTemplateColumns: 'minmax(150px,auto) minmax(0,1fr) 40px 14px', gap: 12, alignItems: 'center', width: '100%', background: 'none', border: 0, borderTop: '1px solid var(--border)', padding: '9px 16px', textAlign: 'left', color: 'var(--text)', cursor: 'pointer' }}
              >
                <span><span className={`pill ${t.cls}`}><span aria-hidden="true">{t.icon}</span>{t.label}</span></span>
                <span className="text-dim" style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.todo}</span>
                <span style={{ fontWeight: 700, textAlign: 'right' }}>{t.count}</span>
                <span aria-hidden="true" className="text-dim">›</span>
              </button>
            ))}
          </section>

          <section className="card" aria-labelledby="h-fix" style={{ padding: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 16px 10px' }}>
              <h2 id="h-fix" style={{ fontSize: 15, margin: 0, flex: 1 }}>Recently fixed</h2>
              <button className="btn btn-sm" onClick={() => navigate('/files?sync_kind=fixed&flag=ok')}>All fixed</button>
            </div>
            {recent.length === 0 && (
              <div className="text-dim" style={{ padding: '6px 16px 18px' }}>Nothing has needed fixing yet.</div>
            )}
            {recent.map((f) => (
              <button
                key={f.id}
                onClick={() => navigate(`/files/${f.id}`)}
                data-hover
                style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: '2px 12px', width: '100%', background: 'none', border: 0, borderTop: '1px solid var(--border)', padding: '8px 16px', textAlign: 'left', color: 'var(--text)', cursor: 'pointer' }}
              >
                <span style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {fileDisplayName(f)}{f.lang ? <span className="text-dim" style={{ fontWeight: 400 }}> · {languageName(f.lang)}</span> : null}
                </span>
                <span className="text-dim" style={{ fontSize: 12, textAlign: 'right' }} title={formatExact(f.last_processed)}>
                  {formatRelative(f.last_processed)}
                </span>
                <span style={{ fontSize: 12.5, color: 'var(--green)', gridColumn: '1 / -1' }}>
                  <span aria-hidden="true">✓ </span>{changeLines(f)[0] ?? syncText(f)}
                </span>
              </button>
            ))}
          </section>
        </Grid2>
      </div>
    </>
  )
}
