import type { RunRow } from '../api/types'

// A run is displayed as "single" whenever it only ever touches one movie/show — that's true for
// an actual mode:'single' run (one file), but ALSO for a sweep scoped to one title (Library
// page's per-row Scan). Only a whole-library/whole-kind sweep is "sweep".
export function runTypeLabel(r: Pick<RunRow, 'mode' | 'target_title'>): 'single' | 'sweep' {
  if (r.mode === 'single') return 'single'
  return r.target_title ? 'single' : 'sweep'
}

export function runTargetLabel(r: RunRow): string {
  if (runTypeLabel(r) === 'single') {
    return r.target_title ?? '—'
  }
  const scope = r.force ? 'Full' : 'Only missing files'
  return r.target_kind ? `${scope} (${r.target_kind})` : scope
}
