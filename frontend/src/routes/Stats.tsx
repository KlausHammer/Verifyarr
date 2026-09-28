import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { MatchRatePoint, Paginated, RunRow, StatsSummary } from '../api/types'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { languageName } from '../lib/languages'
import { formatExact, formatRelative } from '../lib/format'
import { runTargetLabel, runTypeLabel } from '../lib/runLabels'

const SCORE_BUCKETS = [
  { bucket: '0-20%', color: 'var(--red)' },
  { bucket: '20-40%', color: 'var(--red)' },
  { bucket: '40-60%', color: 'var(--yellow)' },
  { bucket: '60-80%', color: 'var(--yellow)' },
  { bucket: '80-100%', color: 'var(--green)' },
]

function MatchRateChart({ points }: { points: MatchRatePoint[] }) {
  if (points.length === 0) return <div className="text-dim">No checks in the last 30 days.</div>

  const rated = points.map((p) => ({ ...p, rate: p.total > 0 ? (p.ok_count / p.total) * 100 : 0 }))
  const first = new Date(`${rated[0].period}T00:00:00`).getTime()
  const span = Math.max(1, new Date(`${rated[rated.length - 1].period}T00:00:00`).getTime() - first)
  const x = (period: string) => 44 + ((new Date(`${period}T00:00:00`).getTime() - first) / span) * 366
  // Axis floor follows the data: a fixed 75% floor drew a 30% library at 75%.
  const lo = Math.min(75, Math.floor(Math.min(...rated.map((p) => p.rate)) / 5) * 5)
  const y = (r: number) => 10 + ((100 - Math.max(lo, Math.min(100, r))) / (100 - lo)) * 170
  const pts = rated.map((p) => `${x(p.period).toFixed(1)},${y(p.rate).toFixed(1)}`).join(' ')
  const fmt = (period: string) => new Date(`${period}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  const last = rated[rated.length - 1]
  const lastIsToday = new Date().toISOString().slice(0, 10) === last.period
  const mid = rated[Math.floor(rated.length / 2)]

  return (
    <svg viewBox="0 0 420 214" role="img" aria-label={`Match rate ${rated.length > 1 ? `went from ${rated[0].rate.toFixed(1)}% to ${last.rate.toFixed(1)}%` : `is ${last.rate.toFixed(1)}%`} over the last 30 days`} style={{ width: '100%', height: 'auto', display: 'block', fontFamily: 'var(--font)' }}>
      <g stroke="#2a2f3d" strokeWidth="1">
        {[10, 44, 78, 112, 146, 180].map((yy) => (
          <line key={yy} x1="44" y1={yy} x2="410" y2={yy} />
        ))}
      </g>
      <g fill="#8b90a3" fontSize="12" textAnchor="end">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <text key={i} x="32" y={14 + i * 34}>{Math.round(100 - (i * (100 - lo)) / 5)}%</text>
        ))}
      </g>
      {rated.length > 1 && <path d={`M44,180 L${pts.split(' ').join(' L')} L410,180 Z`} fill="rgba(94,234,212,.10)" />}
      {rated.length > 1 && <polyline points={pts} fill="none" stroke="#5eead4" strokeWidth="2" strokeLinejoin="round" />}
      <circle cx={x(last.period)} cy={y(last.rate)} r="4" fill="#5eead4" />
      <g fill="#8b90a3" fontSize="12">
        <text x="44" y="204">{fmt(rated[0].period)}</text>
        {rated.length > 2 && <text x="227" y="204" textAnchor="middle">{fmt(mid.period)}</text>}
        <text x="410" y="204" textAnchor="end">{lastIsToday ? 'Today' : fmt(last.period)} · {last.rate.toFixed(1)}%</text>
      </g>
    </svg>
  )
}

export default function Stats() {
  const [summary, setSummary] = useState<StatsSummary | null>(null)
  const [points, setPoints] = useState<MatchRatePoint[]>([])
  const [runs, setRuns] = useState<RunRow[]>([])
  const [failed, setFailed] = useState(false)
  const [starting, setStarting] = useState(false)
  const { isRunning, refresh: refreshRunning } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  const load = useCallback(async () => {
    setFailed(false)
    try {
      const [s, mr, r] = await Promise.all([
        api.get<StatsSummary>('/stats/summary'),
        api.get<{ items: MatchRatePoint[] }>('/stats/match-rate?group_by=day&days=30'),
        api.get<Paginated<RunRow>>('/runs?page_size=5'),
      ])
      setSummary(s)
      setPoints(mr.items)
      setRuns(r.items)
    } catch {
      setSummary(null)
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function scan() {
    setStarting(true)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep' })
      refreshRunning()
      navigate(`/activity/${r.run_id}`)
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setStarting(false)
    }
  }

  if (failed) return <ErrorState title="Stats" noun="stats" onRetry={load} />
  if (!summary) return <LoadingState title="Stats" noun="stats" />

  const h = summary.health
  const n = (v: number | null) => v ?? 0
  const checked = summary.files.total - n(h.missing) - n(h.skipped)
  const good = n(h.insync) + n(h.fixed) + n(h.generated)
  const pct = checked ? Math.round((good / checked) * 1000) / 10 : 0

  const distMax = Math.max(1, ...SCORE_BUCKETS.map((b) => summary.score_distribution.find((d) => d.bucket === b.bucket)?.n ?? 0))
  const byKind = (kind: string) => summary.by_kind.find((k) => k.kind === kind)
  const kindCard = (kind: string, label: string) => {
    const row = byKind(kind)
    if (!row) return null
    const t = row.ok + row.fixed + row.suspect || 1
    return {
      label,
      pct: row.n ? Math.round(((row.ok + row.fixed) / row.n) * 1000) / 10 : 0,
      a: row.ok, b: row.fixed, c: row.suspect, missing: row.missing,
      w1: (row.ok / t) * 100, w2: (row.fixed / t) * 100, w3: (row.suspect / t) * 100,
    }
  }
  const mvs = [kindCard('movie', 'Movies'), kindCard('series', 'Series')].filter((m) => m !== null)

  return (
    <div>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17, flex: 1 }}>Stats</h1>
        <span className="text-dim" style={{ fontSize: 13 }}>Last 30 days</span>
      </div>

      <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
        {summary.files.total === 0 && (
          <div className="card" style={{ maxWidth: 600 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>No stats yet</h2>
            <p className="text-dim" style={{ margin: '0 0 14px', lineHeight: 1.5 }}>Stats appear after the first scan has checked your library.</p>
            <button className="btn btn-primary" onClick={scan} disabled={isRunning || starting}>
              {isRunning ? 'Scan running…' : starting ? <span className="spinner" /> : 'Scan library'}
            </button>
          </div>
        )}

        {summary.files.total > 0 && (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,200px),1fr))', gap: 16 }}>
              {[
                { label: 'Subtitles checked', value: String(checked), sub: `${n(h.videos)} videos` },
                { label: 'In sync now', value: `${pct}%`, sub: `${good} subtitles` },
                { label: 'Fixed by Verifyarr', value: String(n(h.fixed)), sub: 'moved or rescaled' },
                { label: 'Flagged', value: String(n(h.suspect)), sub: 'could not be fixed safely' },
              ].map((t) => (
                <div key={t.label} className="card">
                  <div className="text-dim" style={{ fontSize: 12.5 }}>{t.label}</div>
                  <div style={{ fontSize: 26, fontWeight: 700, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>{t.value}</div>
                  <div className="text-dim" style={{ fontSize: 12, marginTop: 2 }}>{t.sub}</div>
                </div>
              ))}
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,340px),1fr))', gap: 16 }}>
              <section className="card" aria-labelledby="h-mr">
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
                  <h2 id="h-mr" style={{ margin: 0, fontSize: 15, flex: 1 }}>Match rate over time</h2>
                  <span className="text-dim" style={{ fontSize: 12.5 }}>share of checked subtitles in sync</span>
                </div>
                <MatchRateChart points={points} />
              </section>

              <section className="card" aria-labelledby="h-sd">
                <h2 id="h-sd" style={{ margin: '0 0 4px', fontSize: 15 }}>Score distribution</h2>
                <p className="text-dim" style={{ margin: '0 0 12px', fontSize: 12.5 }}>How closely each subtitle&apos;s text matches the speech (0 = nothing, 1 = everything).</p>
                <div role="list" style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 150, borderBottom: '1px solid var(--border)' }}>
                  {SCORE_BUCKETS.map((b) => {
                    const c = summary.score_distribution.find((d) => d.bucket === b.bucket)?.n ?? 0
                    const frac = c / distMax
                    return (
                      <div key={b.bucket} role="listitem" aria-label={`Score ${b.bucket.replace('-', ' to ')}%: ${c} subtitles`} style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', alignItems: 'center', height: '100%', gap: 3 }}>
                        <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{c}</span>
                        <svg aria-hidden="true" viewBox="0 0 10 100" preserveAspectRatio="none" width="100%" style={{ flex: 1, display: 'block' }}>
                          <rect x="0" y={100 - frac * 88} width="10" height={frac * 88} style={{ fill: b.color }} />
                        </svg>
                      </div>
                    )
                  })}
                </div>
                <div aria-hidden="true" style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                  {SCORE_BUCKETS.map((b) => (
                    <span key={b.bucket} style={{ flex: 1, textAlign: 'center', fontSize: 10.5, color: 'var(--text-dim)' }}>{b.bucket}</span>
                  ))}
                </div>
              </section>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,280px),1fr))', gap: 16 }}>
              {summary.by_lang.length > 0 && (
                <section className="card" aria-labelledby="h-bl">
                  <h2 id="h-bl" style={{ margin: '0 0 12px', fontSize: 15 }}>Average score by language</h2>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {summary.by_lang.map((l) => (
                      <div key={l.lang}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 3 }}>
                          <span>{languageName(l.lang)} <span className="text-dim">· {l.n} subtitles</span></span>
                          <span style={{ fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
                            {l.avg_score !== null ? l.avg_score.toFixed(2) : '—'}
                          </span>
                        </div>
                        <div aria-hidden="true" style={{ height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden' }}>
                          <svg aria-hidden="true" width="100%" height="6" style={{ display: 'block' }}>
                            <rect x="0" y="0" width={`${(l.avg_score ?? 0) * 100}%`} height="6" style={{ fill: 'var(--blue)' }} />
                          </svg>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {mvs.length > 0 && (
                <section className="card" aria-labelledby="h-mv">
                  <h2 id="h-mv" style={{ margin: '0 0 12px', fontSize: 15 }}>Movies vs series</h2>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {mvs.map((m) => m && (
                      <div key={m.label}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 4 }}>
                          <span style={{ fontWeight: 600 }}>{m.label}</span>
                          <span className="text-dim">{m.pct}% in sync</span>
                        </div>
                        <div aria-hidden="true" style={{ display: 'flex', height: 10, borderRadius: 3, overflow: 'hidden', gap: 2, background: 'var(--border)' }}>
                          <svg width="100%" height="10" style={{ display: 'block', flex: 1 }}>
                            <rect x="0" y="0" width={`${m.w1}%`} height="10" style={{ fill: 'var(--green)' }} />
                            <rect x={`${m.w1}%`} y="0" width={`${m.w2}%`} height="10" style={{ fill: 'var(--accent-dim)' }} />
                            <rect x={`${m.w1 + m.w2}%`} y="0" width={`${m.w3}%`} height="10" style={{ fill: 'var(--red)' }} />
                          </svg>
                        </div>
                        <div className="text-dim" style={{ fontSize: 12.5, marginTop: 4 }}>
                          {m.a} in sync · {m.b} fixed · {m.c} flagged{m.missing > 0 ? ` · ${m.missing} missing` : ''}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="card" aria-labelledby="h-sw">
                <h2 id="h-sw" style={{ margin: '0 0 12px', fontSize: 15 }}>Swapped lines</h2>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div>
                    <div style={{ fontSize: 24, fontWeight: 700 }}>{summary.files.line_order_fixed_total ?? 0}</div>
                    <div className="text-dim" style={{ fontSize: 13 }}>swapped line pairs put back in order</div>
                  </div>
                  <div>
                    <div style={{ fontSize: 24, fontWeight: 700, color: (summary.files.line_order_flagged_total ?? 0) > 0 ? 'var(--red)' : undefined }}>
                      {summary.files.line_order_flagged_total ?? 0}
                    </div>
                    <div className="text-dim" style={{ fontSize: 13 }}>pairs flagged as &quot;Lines out of order&quot;: too many to fix safely</div>
                  </div>
                  {(summary.files.line_order_flagged_total ?? 0) > 0 && (
                    <button className="btn btn-sm" style={{ alignSelf: 'flex-start' }} onClick={() => navigate('/files?reason=lines_out_of_order')}>
                      Show them
                    </button>
                  )}
                </div>
              </section>
            </div>

            <section className="card" aria-labelledby="h-rj" style={{ padding: 0, overflow: 'hidden' }}>
              <div style={{ display: 'flex', alignItems: 'center', padding: '14px 16px 10px' }}>
                <h2 id="h-rj" style={{ margin: 0, fontSize: 15, flex: 1 }}>Recent jobs</h2>
                <button className="btn btn-sm" onClick={() => navigate('/activity')}>All activity</button>
              </div>
              {runs.length === 0 && (
                <div className="text-dim" style={{ padding: '6px 16px 16px', fontSize: 13 }}>No jobs yet.</div>
              )}
              {runs.map((j) => (
                <button
                  key={j.id}
                  onClick={() => navigate(`/activity/${j.id}`)}
                  data-hover
                  style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,2fr) 96px 110px', gap: 12, alignItems: 'center', width: '100%', background: 'none', border: 0, borderTop: '1px solid var(--border)', padding: '8px 16px', textAlign: 'left', color: 'var(--text)', fontSize: 13, cursor: 'pointer' }}
                >
                  <span style={{ fontWeight: 500 }}>{runTypeLabel(j)}</span>
                  <span className="text-dim" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {runTargetLabel(j)} · {j.files_processed} files, {j.files_changed} changed, {j.files_suspect} flagged
                  </span>
                  <StatusPill value={j.status} />
                  <span className="text-dim" title={formatExact(j.started_at)}>{formatRelative(j.started_at)}</span>
                </button>
              ))}
            </section>
          </>
        )}
      </div>
    </div>
  )
}
