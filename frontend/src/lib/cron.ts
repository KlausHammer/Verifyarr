export type ScheduleMode = 'daily' | 'weekly' | 'advanced'

export interface FriendlySchedule {
  mode: ScheduleMode
  time: string // "HH:MM"
  dayOfWeek: number // 0=Sunday .. 6=Saturday
}

export const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

/** Best-effort parse of a 5-field cron string into the simple daily/weekly shape the picker
 * understands. Anything more complex (step values, ranges, multiple days, non-* month/day-of-month)
 * falls back to 'advanced' mode, where the raw cron string is edited directly. */
export function parseCron(cron: string): FriendlySchedule {
  const fallback: FriendlySchedule = { mode: 'advanced', time: '04:00', dayOfWeek: 0 }
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return fallback
  const [m, h, dom, mon, dow] = parts
  if (dom !== '*' || mon !== '*') return fallback
  if (!/^\d+$/.test(m) || !/^\d+$/.test(h)) return fallback
  const hour = Number(h)
  const minute = Number(m)
  if (hour > 23 || minute > 59) return fallback
  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  if (dow === '*') return { mode: 'daily', time, dayOfWeek: 0 }
  if (/^[0-6]$/.test(dow)) return { mode: 'weekly', time, dayOfWeek: Number(dow) }
  return fallback
}

export function buildCron(schedule: Omit<FriendlySchedule, 'mode'> & { mode: 'daily' | 'weekly' }): string {
  const [h, m] = schedule.time.split(':').map((x) => Number(x))
  const hour = Number.isFinite(h) ? h : 0
  const minute = Number.isFinite(m) ? m : 0
  if (schedule.mode === 'daily') return `${minute} ${hour} * * *`
  return `${minute} ${hour} * * ${schedule.dayOfWeek}`
}
