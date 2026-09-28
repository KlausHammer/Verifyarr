import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { BlacklistAction, Paginated } from '../api/types'
import { ErrorState, LoadingState } from '../components/PageState'
import { fileName, formatExact, formatRelative } from '../lib/format'
import { languageName } from '../lib/languages'

// The whole list (capped) is fetched once so the columns sort honestly client-side.
const PAGE_SIZE = 500

const HEADS = [
  { key: 'file', label: 'File' },
  { key: 'language', label: 'Language' },
  { key: 'provider', label: 'Provider' },
  { key: 'outcome', label: 'Outcome' },
  { key: 'time', label: 'Time' },
] as const

type SortKey = (typeof HEADS)[number]['key']

function outcomeOf(b: BlacklistAction): { cls: string; icon: string; label: string } {
  if (!b.remediation_outcome) return { cls: 'pill-muted', icon: '–', label: 'Waiting for Bazarr' }
  if (b.remediation_outcome.includes('rejected')) {
    return { cls: 'pill-bad', icon: '✕', label: 'No replacement passed' }
  }
  return { cls: 'pill-muted', icon: '–', label: b.remediation_outcome }
}

function sortVal(b: BlacklistAction, key: SortKey): string {
  switch (key) {
    case 'file': return fileName(b.video_path ?? b.subtitle_path).toLowerCase()
    case 'language': return (b.language ?? '').toLowerCase()
    case 'provider': return (b.provider ?? '').toLowerCase()
    case 'outcome': return (b.remediation_outcome ?? '').toLowerCase()
    case 'time': return b.blacklisted_at
  }
}

export default function BazarrBlacklist() {
  const [data, setData] = useState<Paginated<BlacklistAction> | null>(null)
  const [failed, setFailed] = useState(false)
  const [sort, setSort] = useState<SortKey>('time')
  const [dir, setDir] = useState<1 | -1>(-1)

  function load() {
    setFailed(false)
    api
      .get<Paginated<BlacklistAction>>(`/bazarr/blacklist?page=1&page_size=${PAGE_SIZE}`)
      .then(setData)
      .catch(() => {
        setData(null)
        setFailed(true)
      })
  }

  useEffect(load, [])

  if (failed) return <ErrorState title="Bazarr blacklist" noun="the blacklist" onRetry={load} />
  if (!data) return <LoadingState title="Bazarr blacklist" noun="the blacklist" />

  const rows = data.items.slice().sort((a, b) => {
    const av = sortVal(a, sort)
    const bv = sortVal(b, sort)
    return (av < bv ? -1 : av > bv ? 1 : 0) * dir
  })

  function toggleSort(key: SortKey) {
    if (sort === key) setDir((d) => (d === 1 ? -1 : 1))
    else {
      setSort(key)
      setDir(-1)
    }
  }

  return (
    <div>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, boxSizing: 'border-box', borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17 }}>Bazarr blacklist</h1>
        <span className="text-dim" style={{ fontSize: 13, flex: 1 }}>
          {data.total ? `${data.total} subtitles` : ''}
        </span>
      </div>

      <div data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <p className="text-dim" style={{ margin: 0, fontSize: 13, lineHeight: 1.5, maxWidth: 760 }}>
          Subtitles Verifyarr reported to Bazarr as bad. Bazarr won&apos;t download them again and looks for another one; Verifyarr tests whatever Bazarr finds.
        </p>
        {data.total > data.items.length && (
          <p className="text-dim" style={{ margin: 0, fontSize: 13 }}>
            Showing the latest {data.items.length} of {data.total}.
          </p>
        )}

        {rows.length === 0 && (
          <div className="card" style={{ maxWidth: 600 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>Nothing blacklisted</h2>
            <p className="text-dim" style={{ margin: 0, lineHeight: 1.5 }}>
              Verifyarr hasn&apos;t reported any subtitle to Bazarr yet. This happens when you press Blacklist, or automatically if bad subtitles are set to be blacklisted or replaced.
            </p>
          </div>
        )}

        {rows.length > 0 && (
          <div role="table" aria-label="Blacklisted subtitles" className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div role="row" data-head style={{ display: 'grid', gridTemplateColumns: 'minmax(0,2fr) 100px 130px minmax(0,1.6fr) 110px', gap: 12, padding: '8px 16px', borderBottom: '1px solid var(--border)', fontSize: 12.5, color: 'var(--text-dim)', fontWeight: 600 }}>
              {HEADS.map((hd) => (
                <div key={hd.key} role="columnheader" aria-sort={sort === hd.key ? (dir > 0 ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => toggleSort(hd.key)} data-hover style={{ background: 'none', border: 0, padding: 0, fontWeight: 600, fontSize: 12.5, color: sort === hd.key ? 'var(--text)' : 'inherit', cursor: 'pointer' }}>
                    {hd.label}<span aria-hidden="true">{sort === hd.key ? (dir > 0 ? ' ▲' : ' ▼') : ''}</span>
                  </button>
                </div>
              ))}
            </div>
            {rows.map((b) => {
              const o = outcomeOf(b)
              return (
                <div key={b.id} role="row" data-row data-hover style={{ display: 'grid', gridTemplateColumns: 'minmax(0,2fr) 100px 130px minmax(0,1.6fr) 110px', gap: 12, alignItems: 'center', padding: '8px 16px', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                  <div role="cell" data-cell="main" style={{ fontWeight: 500, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={b.video_path ?? b.subtitle_path ?? ''}>
                    {fileName(b.video_path ?? b.subtitle_path)}
                  </div>
                  <div role="cell" data-cell data-label="Language">{b.language ? languageName(b.language) : '—'}</div>
                  <div role="cell" data-cell data-label="Provider" className="mono" style={{ fontSize: 12.5 }}>{b.provider ?? '—'}</div>
                  <div role="cell" data-cell data-label="Outcome" title={b.remediation_outcome ?? ''}>
                    <span className={`pill ${o.cls}`}><span aria-hidden="true">{o.icon}</span>{o.label}</span>
                  </div>
                  <div role="cell" data-cell data-label="Time" style={{ color: 'var(--text-dim)' }} title={formatExact(b.blacklisted_at)}>
                    {formatRelative(b.blacklisted_at)}
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
