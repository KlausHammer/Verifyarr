import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { SeriesEpisodes } from '../api/types'
import { ErrorState, LoadingState } from '../components/PageState'
import StatusPill from '../components/StatusPill'
import { useRunningJob } from '../hooks/useRunningJob'
import { useToasts } from '../hooks/useToasts'
import { formatRelative } from '../lib/format'

export default function SeriesDetail() {
  const { title = '' } = useParams()
  const [data, setData] = useState<SeriesEpisodes | null>(null)
  const [failed, setFailed] = useState(false)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const { isRunning, refresh } = useRunningJob()
  const { toast } = useToasts()
  const navigate = useNavigate()

  function load() {
    setFailed(false)
    api.get<SeriesEpisodes>(`/library/series/episodes?title=${encodeURIComponent(title)}`)
      .then(setData)
      .catch(() => { setData(null); setFailed(true) })
  }
  useEffect(load, [title])

  // season / episode omitted = the whole series.
  async function scan(key: string, season?: string, episode?: string) {
    setBusyKey(key)
    try {
      const r = await api.post<{ run_id: number }>('/runs', { mode: 'sweep', kind: 'series', title, season, episode })
      refresh()
      toast(`Scan started for ${[title, episode ?? season].filter(Boolean).join(' ')}.`, {
        kind: 'info', action: () => navigate(`/activity/${r.run_id}`), actionLabel: 'View job',
      })
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), { kind: 'bad' })
    } finally {
      setBusyKey(null)
    }
  }

  if (failed) return <ErrorState title={title} noun="episodes" onRetry={load} />
  if (!data) return <LoadingState title={title} noun="episodes" />
  const busy = busyKey !== null || isRunning
  const count = data.seasons.reduce((s, x) => s + x.episodes.length, 0)

  return (
    <div>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <button className="btn btn-sm" onClick={() => navigate('/series')}>← Series</button>
        <h1 style={{ margin: 0, fontSize: 17 }}>{data.title}</h1>
        <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>{data.seasons.length} seasons · {count} episodes</span>
        <button className="btn" disabled={busy} onClick={() => scan('all')}>
          {busyKey === 'all' ? <span className="spinner" /> : 'Scan whole series'}
        </button>
      </div>
      <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
        {data.seasons.map((s) => (
          <section key={s.season} className="card" style={{ padding: 0 }} aria-label={`Season ${s.season}`}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
              <h2 style={{ margin: 0, fontSize: 15, flex: 1 }}>
                {/^S\d+$/.test(s.season) ? `Season ${Number(s.season.slice(1))}` : 'Other files'}
                <span className="text-dim" style={{ fontWeight: 400, fontSize: 13 }}> · {s.episodes.length} episodes</span>
              </h2>
              {/^S\d+$/.test(s.season) && (
                <button className="btn btn-sm" disabled={busy} onClick={() => scan(s.season, s.season)}>
                  {busyKey === s.season ? <span className="spinner" /> : 'Scan season'}
                </button>
              )}
            </div>
            {s.episodes.map((e) => {
              const key = e.video_path
              return (
                <div key={key} style={{ display: 'grid', gridTemplateColumns: '70px minmax(0,1fr) auto 80px', gap: 12, alignItems: 'center', padding: '7px 14px', borderBottom: '1px solid var(--border)' }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{e.season_episode ?? '—'}</div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13.5 }} title={e.name}>{e.name}</div>
                    {e.embedded_langs.length > 0 && (
                      <span className="text-dim" style={{ fontSize: 12.5 }}>Embedded in the video: {e.embedded_langs.join(', ')}</span>
                    )}
                    {e.subtitles.length === 0 && e.embedded_langs.length === 0 && (
                      <span className="text-dim" style={{ fontSize: 12.5 }}>{e.has_subtitle ? 'Has a subtitle (embedded)' : 'No subtitle'}</span>
                    )}
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end' }}>
                    {e.subtitles.map((f) => (
                      <button key={f.id} onClick={() => navigate(`/files/${f.id}`)} data-hover
                        style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', display: 'flex', gap: 6, alignItems: 'center' }}
                        title={f.subtitle_path}>
                        <span className="text-dim" style={{ fontSize: 12 }}>{f.lang ?? '?'}</span>
                        <StatusPill value={f.correctness_flag === 'SUSPECT' ? 'SUSPECT' : f.sync_status} />
                        <span className="text-dim" style={{ fontSize: 12 }}>{formatRelative(f.last_processed)}</span>
                      </button>
                    ))}
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    {e.season_episode && (
                      <button className="btn btn-sm" disabled={busy} onClick={() => scan(key, s.season, e.season_episode ?? undefined)} aria-label={`Scan ${e.season_episode}`}>
                        {busyKey === key ? <span className="spinner" /> : 'Scan'}
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </section>
        ))}
      </div>
    </div>
  )
}
