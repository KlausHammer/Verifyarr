import type { FileRow, RunRow } from '../api/types'

// Plain-language job labels from the run's own (mode, trigger, target, force). Manual
// one-file actions (re-check, fetch replacement, quarantine, blacklist) all run as
// mode 'single', so they share one label — the API doesn't record which action it was.
export function runTypeLabel(r: Pick<RunRow, 'mode' | 'trigger' | 'target_title' | 'force'>): string {
  if (r.mode === 'generate_single') return 'Generate subtitle'
  if (r.mode === 'single') return 'Check file'
  if (r.trigger === 'scheduled') return 'Scheduled scan'
  if (r.trigger === 'bazarr_poll') return 'Bazarr scan'
  if (r.target_title) return r.force ? 'Rescan title' : 'Scan title'
  if (r.force) return 'Rescan everything'
  return 'Scan library'
}

export function runTargetLabel(r: RunRow): string {
  if (r.mode === 'single' || r.mode === 'generate_single') {
    return r.target_title ?? '—'
  }
  if (r.target_title) return r.target_title
  const scope = r.target_kind === 'movie' ? 'All movies'
    : r.target_kind === 'series' ? 'All series'
    : 'Whole library'
  const what = r.force ? 'everything' : 'new and changed files'
  return `${scope} · ${what}${r.dry_run ? ' · dry run' : ''}`
}

/** "Title S01E02" / "Title" — the label a single-file run stores as its target. */
export function fileRunLabel(f: Pick<FileRow, 'series_or_movie_title' | 'season_episode'>): string | null {
  if (!f.series_or_movie_title) return null
  return f.season_episode ? `${f.series_or_movie_title} ${f.season_episode}` : f.series_or_movie_title
}

/** Best-effort: is this running job working on this exact file? Only answerable for
 * single-file runs (a sweep's current file isn't recorded), so anything else is "no". */
export function runMatchesFile(run: RunRow, f: Pick<FileRow, 'series_or_movie_title' | 'season_episode'>): boolean {
  if (run.status !== 'running') return false
  if (run.mode !== 'single' && run.mode !== 'generate_single') return false
  const label = fileRunLabel(f)
  return label !== null && run.target_title === label
}
