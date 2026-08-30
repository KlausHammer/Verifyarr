import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Paginated, RunRow } from '../api/types'

/** Polls which job (if any) is currently running — used for the topbar badge and to
 * disable "run now" buttons elsewhere while something is already running. */
export function useRunningJob(intervalMs = 4000) {
  const [currentRunId, setCurrentRunId] = useState<number | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    api
      .get<Paginated<RunRow> & { current_run_id: number | null }>('/runs?page_size=1')
      .then((r) => {
        if (!cancelled) setCurrentRunId(r.current_run_id)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [tick])

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])

  return { currentRunId, isRunning: currentRunId !== null, refresh: () => setTick((t) => t + 1) }
}
