const FLAG_MAP: Record<string, { cls: string; label?: string }> = {
  ok: { cls: 'pill-ok' },
  SUSPECT: { cls: 'pill-bad' },
  missing: { cls: 'pill-bad' },
  skipped: { cls: 'pill-muted' },
  disabled: { cls: 'pill-muted' },
  running: { cls: 'pill-info' },
  completed: { cls: 'pill-ok' },
  cancelled: { cls: 'pill-warn' },
  failed: { cls: 'pill-bad' },
  'already in sync': { cls: 'pill-ok' },
}

export default function StatusPill({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="pill pill-muted">—</span>
  const known = FLAG_MAP[value]
  if (known) return <span className={`pill ${known.cls}`}>{known.label ?? value}</span>
  if (value.startsWith('fixed')) return <span className="pill pill-ok">{value}</span>
  if (value.startsWith('error') || value === 'unexpected-error') return <span className="pill pill-bad">{value}</span>
  if (value.startsWith('would')) return <span className="pill pill-info">{value}</span>
  return <span className="pill pill-muted">{value}</span>
}
