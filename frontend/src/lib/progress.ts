import type { RunRow } from '../api/types'

/** Finished files plus the part-done ones, as a whole number of files. */
export function filesDone(run: Pick<RunRow, 'files_processed' | 'files_inflight'>): number {
  return run.files_processed + (run.files_inflight ?? 0)
}

/** Percentage of a run. While it runs it stops at 99: steps after listening (sync, checks, a
 * replacement download) can still add work, so it must never show 100 before the run is over. */
export function runPct(run: Pick<RunRow, 'files_processed' | 'files_inflight' | 'files_total' | 'status'>): number | null {
  if (!run.files_total) return null
  const pct = Math.round((filesDone(run) / run.files_total) * 100)
  return Math.max(0, Math.min(run.status === 'running' ? 99 : 100, pct))
}
