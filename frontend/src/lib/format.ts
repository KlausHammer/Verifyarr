/** Relative time ("3 min ago", "yesterday") — pair with formatExact on hover. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return iso
  const diffMs = Date.now() - ts
  const abs = Math.abs(diffMs)
  const future = diffMs < 0
  if (abs < 45000) return future ? 'in a moment' : 'just now'
  const MIN = 60000
  const HOUR = 3600000
  const DAY = 86400000
  let s: string
  if (abs < HOUR) s = `${Math.round(abs / MIN)} min`
  else if (abs < DAY) s = `${Math.round(abs / HOUR)} h`
  else if (abs < 2 * DAY && !future) return 'yesterday'
  else if (abs < 30 * DAY) s = `${Math.round(abs / DAY)} days`
  else {
    return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
  }
  return future ? `in ${s}` : `${s} ago`
}

/** Exact timestamp for title="" hover on every relative time. */
export function formatExact(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-US', {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
}

/** "14:03:22" clock time for log lines. */
export function formatClock(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let val = n
  let i = -1
  do {
    val /= 1024
    i++
  } while (val >= 1024 && i < units.length - 1)
  return `${val.toFixed(1)} ${units[i]}`
}

export function fileName(path: string | null | undefined): string {
  if (!path) return '—'
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

export function durationBetween(startIso: string, endIso: string | null): string {
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  return formatDuration(Math.max(0, Math.round((end - start) / 1000)))
}

/** "Tonight at 03:00" / "Today at 14:00" / "Tomorrow at 09:30" for the next scan. */
export function nextScanLabel(iso: string | null | undefined): string {
  if (!iso) return 'Not scheduled'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const today = new Date()
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  const tomorrow = new Date(today)
  tomorrow.setDate(tomorrow.getDate() + 1)
  if (sameDay(d, today)) return `${d.getHours() < 5 ? 'Tonight' : 'Today'} at ${time}`
  if (sameDay(d, tomorrow)) return `Tomorrow at ${time}`
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) + ` at ${time}`
}

/** "45 s" / "3 min 20 s" / "2 h 5 min". */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds))
  if (s < 60) return `${s} s`
  if (s < 3600) {
    const m = Math.floor(s / 60)
    const r = s % 60
    return r ? `${m} min ${r} s` : `${m} min`
  }
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return m ? `${h} h ${m} min` : `${h} h`
}
