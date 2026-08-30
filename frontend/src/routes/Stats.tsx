import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { MatchRatePoint, Paginated, RunRow, StatsSummary } from '../api/types'
import { languageName } from '../lib/languages'
import { formatRelative } from '../lib/format'
import { useRunningJob } from '../hooks/useRunningJob'
import StatusPill from '../components/StatusPill'

function MatchRateChart({ points }: { points: MatchRatePoint[] }) {
  if (points.length === 0) return <div className="text-dim">No correctness checks yet.</div>

  const width = Math.max(600, points.length * 34)
  const height = 220
  const padBottom = 30
  const chartH = height - padBottom

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg width={width} height={height}>
        {[0, 0.25, 0.5, 0.75, 1].map((frac) => (
          <line
            key={frac}
            x1={0}
            x2={width}
            y1={chartH - frac * chartH}
            y2={chartH - frac * chartH}
            stroke="var(--border)"
            strokeWidth={1}
          />
        ))}
        {points.map((p, i) => {
          const rate = p.total > 0 ? p.ok_count / p.total : 0
          const barW = 20
          const x = i * (width / points.length) + (width / points.length - barW) / 2
          const barH = rate * chartH
          const color = rate >= 0.8 ? 'var(--green)' : rate >= 0.5 ? 'var(--yellow)' : 'var(--red)'
          return (
            <g key={p.period}>
              <rect x={x} y={chartH - barH} width={barW} height={barH} fill={color} rx={2}>
                <title>
                  {p.period}: {p.ok_count}/{p.total} ok ({Math.round(rate * 100)}%)
                </title>
              </rect>
              <text
                x={x + barW / 2}
                y={height - 10}
                textAnchor="middle"
                fontSize={10}
                fill="var(--text-faint)"
              >
                {p.period.slice(5)}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

// Reusable horizontal-bar row, used for both score distribution and by-language breakdowns.
function BarRow({ label, value, max, display, color }: { label: string; value: number; max: number; display: string; color?: string }) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
      <div style={{ width: 90, fontSize: 12.5 }} className="text-dim">
        {label}
      </div>
      <div style={{ flex: 1, background: 'var(--bg)', borderRadius: 4, height: 16, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color ?? 'var(--accent)', borderRadius: 4 }} />
      </div>
      <div style={{ width: 70, fontSize: 12.5, textAlign: 'right' }} className="mono">
        {display}
      </div>
    </div>
  )
}

function StatCard({ value, label, color }: { value: string | number; label: string; color?: string }) {
  return (
    <div className="card">
      <div style={{ fontSize: 24, fontWeight: 700, color: color ?? 'var(--text)' }}>{value}</div>
      <div className="text-dim" style={{ fontSize: 12.5 }}>
        {label}
      </div>
    </div>
  )
}

const SCORE_BUCKET_ORDER = ['0-20%', '20-40%', '40-60%', '60-80%', '80-100%']

export default function Stats() {
  const [summary, setSummary] = useState<StatsSummary | null>(null)
  const [points, setPoints] = useState<MatchRatePoint[]>([])
  const [groupBy, setGroupBy] = useState<'day' | 'week'>('day')
  const [runs, setRuns] = useState<RunRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const { isRunning, refresh: refreshRunning } = useRunningJob()
  const navigate = useNavigate()

  function loadSummary() {
    api
      .get<StatsSummary>('/stats/summary')
      .then(setSummary)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }

  function loadRuns() {
    api
      .get<Paginated<RunRow>>('/runs?page_size=6')
      .then((r) => setRuns(r.items))
      .catch(() => {})
  }

  useEffect(() => {
    loadSummary()
    loadRuns()
  }, [])

  useEffect(() => {
    api
      .get<{ items: MatchRatePoint[] }>(`/stats/match-rate?group_by=${groupBy}&days=90`)
      .then((r) => setPoints(r.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [groupBy])

  async function runSweepNow() {
    setStarting(true)
    setError(null)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep' })
      refreshRunning()
      navigate(`/activity/${r.run_id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }

  const f = summary?.files
  const movieRow = summary?.by_kind.find((k) => k.kind === 'movie')
  const seriesRow = summary?.by_kind.find((k) => k.kind === 'series')
  const scoreDist = summary?.score_distribution ?? []
  const scoreMax = Math.max(1, ...scoreDist.map((b) => b.n))
  const langMax = Math.max(1, ...(summary?.by_lang.map((l) => l.n) ?? []))
  const lineOrderChecked = (f?.line_order_fixed_total ?? 0) + (f?.line_order_flagged_total ?? 0) > 0

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <h1 style={{ marginBottom: 0 }}>Stats</h1>
        <button className="btn btn-primary" onClick={runSweepNow} disabled={starting || isRunning}>
          {isRunning ? 'Job running…' : starting ? <span className="spinner" /> : 'Run sweep now'}
        </button>
      </div>
      {error && <div className="error-banner">{error}</div>}

      {summary && (
        <div
          style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 14, margin: '18px 0 24px' }}
        >
          <StatCard value={f!.total} label="Total files" />
          <StatCard
            value={f!.total > 0 ? `${Math.round((f!.ok / f!.total) * 100)}%` : '0%'}
            label="Match rate (all files)"
            color="var(--green)"
          />
          <StatCard value={f!.out_of_sync} label="Out of sync (fixed)" color="var(--yellow)" />
          <StatCard value={f!.suspect} label="Wrong subtitle detected" color="var(--red)" />
          <StatCard value={f!.missing} label="Missing subtitle" color="var(--yellow)" />
          <StatCard value={f!.errors} label="Errors" color="var(--red)" />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16, marginBottom: 24 }}>
        {summary && (movieRow || seriesRow) && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Movies vs. Series</h3>
            {[
              { label: 'Movies', row: movieRow },
              { label: 'Series', row: seriesRow },
            ].map(({ label, row }) => (
              <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderTop: '1px solid var(--border)' }}>
                <span>{label}</span>
                <span className="text-dim">
                  {row?.n ?? 0} files
                  {row && row.suspect > 0 && <span style={{ color: 'var(--red)' }}> · {row.suspect} suspect</span>}
                  {row && row.missing > 0 && <span style={{ color: 'var(--yellow)' }}> · {row.missing} missing</span>}
                </span>
              </div>
            ))}
          </div>
        )}

        {summary && scoreDist.length > 0 && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Correctness score distribution</h3>
            {SCORE_BUCKET_ORDER.map((bucket) => {
              const n = scoreDist.find((b) => b.bucket === bucket)?.n ?? 0
              const color = bucket === '80-100%' ? 'var(--green)' : bucket === '60-80%' ? 'var(--yellow)' : 'var(--red)'
              return <BarRow key={bucket} label={bucket} value={n} max={scoreMax} display={`${n} file${n === 1 ? '' : 's'}`} color={color} />
            })}
          </div>
        )}

        {summary && summary.by_lang.length > 0 && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Average score by language</h3>
            {summary.by_lang.map((l) => (
              <BarRow
                key={l.lang}
                label={languageName(l.lang)}
                value={l.n}
                max={langMax}
                display={l.avg_score !== null ? l.avg_score.toFixed(2) : '—'}
              />
            ))}
          </div>
        )}

        {summary && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Line-order check</h3>
            {lineOrderChecked ? (
              <div style={{ display: 'flex', gap: 24 }}>
                <div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--green)' }}>{f!.line_order_fixed_total}</div>
                  <div className="text-dim" style={{ fontSize: 12.5 }}>
                    Auto-fixed
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--yellow)' }}>{f!.line_order_flagged_total}</div>
                  <div className="text-dim" style={{ fontSize: 12.5 }}>
                    Flagged for review
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-dim" style={{ fontSize: 13 }}>
                Off, or no files checked yet — see Settings → Automation → What runs.
              </div>
            )}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Match rate over time</h3>
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as 'day' | 'week')} style={{ width: 140 }}>
            <option value="day">Per day</option>
            <option value="week">Per week</option>
          </select>
        </div>
        <MatchRateChart points={points} />
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Recent runs</h3>
        {runs.length === 0 && <div className="text-dim">No runs yet.</div>}
        {runs.map((r) => (
          <div
            key={r.id}
            style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '9px 0', borderTop: '1px solid var(--border)', fontSize: 13,
            }}
          >
            <div>
              <Link to={`/activity/${r.id}`}>#{r.id}</Link>
              <span className="text-dim"> · {formatRelative(r.started_at)}</span>
            </div>
            <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
              <span className="text-dim">{r.files_processed} files</span>
              <StatusPill value={r.status} />
            </div>
          </div>
        ))}
        {runs.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Link to="/activity">See all activity →</Link>
          </div>
        )}
      </div>
    </div>
  )
}
