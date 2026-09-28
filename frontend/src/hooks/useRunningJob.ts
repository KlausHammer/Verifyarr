import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Paginated, RunRow } from '../api/types'

/** Polls which job (if any) is currently running, plus its row for progress display —
 * used for the sidebar indicator and to disable "run now" buttons while busy. */
export function useRunningJob(intervalMs = 4000) {
  const [currentRunId, setCurrentRunId] = useState<number | null>(null)
  const [run, setRun] = useState<RunRow | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    api
      .get<Paginated<RunRow> & { current_run_id: number | null }>('/runs?page_size=1')
      .then(async (r) => {
        if (cancelled) return
        setCurrentRunId(r.current_run_id)
        if (r.current_run_id === null) {
          setRun(null)
        } else {
          try {
            setRun(await api.get<RunRow>(`/runs/${r.current_run_id}`))
          } catch {
            if (!cancelled) setRun(null)
          }
        }
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

  return { currentRunId, run, isRunning: currentRunId !== null, refresh: () => setTick((t) => t + 1) }
}
