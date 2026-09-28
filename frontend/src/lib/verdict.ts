import type { FileRow, SuspectReason } from '../api/types'

// One function maps a files row to the plain-language verdict every screen shows (pill,
// "What happened", "What to do", action button). Sources, in precedence order:
// sync_status first (missing), then correctness_flag + reason, then the sync shape.
// status/auto_action shapes mirror the backend (pipeline/sync_engine write them,
// db.SYNC_KINDS filters them) — keep the three in agreement.

export type VerdictAction = 'replace' | 'generate' | 'recheck' | 'settings'

export interface Verdict {
  key: string
  cls: 'pill-ok' | 'pill-bad' | 'pill-warn' | 'pill-muted' | 'pill-info'
  icon: string
  label: string
  happened: string
  todo: string
  action: VerdictAction | null
  actionLabel: string
  /** Dry-run conclusion ("would fix …") rather than a written fix. */
  dryRun: boolean
}

const REMEDIATED_PREFIX = 'remediated: '
const REMEDIATION_FAILED_MARKER = 'original kept, marked wrong'

function actionLabelFor(action: VerdictAction | null): string {
  switch (action) {
    case 'replace': return 'Fetch replacement'
    case 'generate': return 'Generate with Whisper'
    case 'recheck': return 'Re-check'
    case 'settings': return 'Open settings'
    default: return ''
  }
}

function make(
  key: string, cls: Verdict['cls'], icon: string, label: string,
  happened: string, todo: string, action: VerdictAction | null, dryRun = false,
): Verdict {
  return { key, cls, icon, label, happened, todo, action, actionLabel: actionLabelFor(action), dryRun }
}

/** "+12.3 s" / "−12.3 s" (design's U+2212 minus). */
export function formatShift(seconds: number): string {
  return `${seconds < 0 ? '−' : '+'}${Math.abs(seconds).toFixed(1)} s`
}

function parseDelta(status: string): number | null {
  const m = status.match(/Δ(-?\d+(?:\.\d+)?)s/)
  return m ? Number(m[1]) : null
}

/** "N sync block(s)" / "N anchor region(s)" in the status (mirrors pipeline._parts_in_repair). */
function parseSections(status: string): number | null {
  const m = status.match(/(\d+) (?:sync block|anchor region)\(s\)/)
  return m ? Number(m[1]) : null
}

/** The framerate pair in "fixed (framerate 23.976 -> 24, …)" / "fixed (rate …)". */
function parseRatePair(status: string): [string, string] | null {
  const m = status.match(/fixed \((?:framerate|rate) (.+?), up to /)
  if (!m) return null
  const parts = m[1].split(' -> ')
  return parts.length === 2 ? [parts[0], parts[1]] : null
}

function wasReplaced(f: FileRow): boolean {
  const a = f.auto_action ?? ''
  return a.startsWith(REMEDIATED_PREFIX) || a.includes(`; ${REMEDIATED_PREFIX}`)
}

function replacementFailed(f: FileRow): boolean {
  return (f.auto_action ?? '').includes(REMEDIATION_FAILED_MARKER)
}

export function verdict(f: FileRow): Verdict {
  const status = f.sync_status ?? ''
  const flag = f.correctness_flag ?? ''

  if (status === 'missing') {
    return make('nosub', 'pill-muted', '–', 'No subtitle',
      'No subtitle for this language.',
      'Generate one with Whisper, or wait for Bazarr.', 'generate')
  }
  if (flag === 'generated') {
    return make('generated', 'pill-info', '●', 'Generated',
      'Made from the audio by Whisper.', 'Nothing.', null)
  }
  if (flag === 'skipped') {
    return make('skipped', 'pill-muted', '–', 'Skipped',
      "The audio is in a language the check doesn't cover.",
      'Nothing, or change the languages in Settings.', 'settings')
  }
  if (flag === 'unknown') {
    if (f.reason === 'no_speech_heard') {
      return make('no_speech', 'pill-warn', '!', 'No speech heard',
        'Speech recognition heard no speech in any sample — music, silence or the wrong audio track.',
        'Check the audio track, then Re-check.', 'recheck')
    }
    if (f.reason === 'check_failed') {
      return make('check_failed', 'pill-warn', '!', 'Check failed',
        'A technical error stopped the check.', 'Re-check.', 'recheck')
    }
    return make('unknown', 'pill-warn', '!', "Couldn't check",
      'Nothing could be verified.', 'Re-check.', 'recheck')
  }
  if (flag === 'SUSPECT') {
    if (replacementFailed(f)) {
      return make('repl_failed', 'pill-bad', '✕', 'Replacement failed',
        'No replacement passed; the original was kept.',
        'Try again later, or pick one in Bazarr.', 'replace')
    }
    switch (f.reason) {
      case 'wrong_subtitle':
        return make('wrong', 'pill-bad', '✕', 'Wrong subtitle',
          'The text is not what is spoken (likely another episode or release).',
          'Fetch a replacement.', 'replace')
      case 'partly_out_of_sync':
        return make('partial', 'pill-bad', '✕', 'Partly out of sync',
          'Part of the episode sits at another offset (often a cut or edited version).',
          'Fetch a replacement.', 'replace')
      case 'missing_lines':
        return make('missing_part', 'pill-bad', '✕', 'Missing lines',
          'No subtitles for a stretch where there is speech.', 'Fetch a replacement.', 'replace')
      case 'past_audio_end':
        return make('past_end', 'pill-bad', '✕', "Doesn't fit this video",
          'Lines run past the end of the audio.', 'Fetch a replacement.', 'replace')
      case 'lines_out_of_order':
        return make('swapped', 'pill-bad', '✕', 'Lines out of order',
          'Many two-line entries are in the wrong order; timing cannot be trusted.',
          'Fetch a replacement.', 'replace')
      case 'unreliable_timing':
        return make('noisy', 'pill-bad', '✕', 'Unreliable timing',
          'Lines are individually off; no single fix exists.', 'Fetch a replacement.', 'replace')
      default:
        return make('needs_attention', 'pill-bad', '✕', 'Needs attention',
          'This subtitle was flagged before Verifyarr recorded why.',
          'Re-check it, or fetch a replacement.', 'recheck')
    }
  }
  if (flag === 'ok') {
    if (wasReplaced(f)) {
      return make('insync', 'pill-ok', '✓', 'In sync',
        'Replaced with a new subtitle that matches the audio.', 'Nothing.', null)
    }
    const dryRun = status.startsWith('would fix')
    if (status.startsWith('fixed (framerate') || status.startsWith('fixed (rate')) {
      const pair = parseRatePair(status)
      const happened = pair
        ? `Made for ${pair[0]} fps; rescaled to ${pair[1]} fps to match this video.`
        : 'Made for another framerate or speed; rescaled.'
      return make('fixed_rate', 'pill-ok', '✓', 'Fixed', happened, 'Nothing.', null, dryRun)
    }
    if (status.startsWith('fixed') || dryRun) {
      const shift = f.sync_max_shift_s ?? parseDelta(status)
      const sections = parseSections(status)
        ?? (f.sync_split_blocks && f.sync_split_blocks > 1 ? f.sync_split_blocks : null)
      let happened = shift !== null
        ? `${dryRun ? 'Would move' : 'Moved'} ${formatShift(shift)} to match the audio`
        : 'Moved to match the audio'
      if (sections) happened += `; ${sections} sections were re-timed separately`
      else happened += '.'
      if (dryRun) happened += ' (Dry run — nothing was changed.)'
      return make('fixed_shift', 'pill-ok', '✓', 'Fixed', happened, 'Nothing.', null, dryRun)
    }
    return make('insync', 'pill-ok', '✓', 'In sync',
      'Timing and text match the audio.', 'Nothing.', null)
  }
  if (flag === 'disabled') {
    return make('disabled', 'pill-muted', '–', 'Not checked',
      'The correctness check is turned off in Settings.',
      'Turn it on to check this subtitle, or leave it.', 'settings')
  }
  const noKey = flag.match(/^no (\S+) API key$/)
  if (noKey) {
    const provider = noKey[1].charAt(0).toUpperCase() + noKey[1].slice(1)
    return make('no_key', 'pill-warn', '!', "Can't check",
      `No ${provider} API key is set, so this subtitle couldn't be checked.`,
      'Add an API key in Settings, then Re-check.', 'settings')
  }
  if (status.startsWith('error') || status === 'unexpected-error') {
    return make('sync_error', 'pill-warn', '!', 'Sync failed',
      'The timing step failed with an error.', 'Re-check.', 'recheck')
  }
  return make('not_checked', 'pill-warn', '!', 'Not checked',
    'No check result is recorded for this file.', 'Re-check.', 'recheck')
}

/** Short sync-column text for the Files table (design's syncText). */
export function syncText(f: FileRow): string {
  const status = f.sync_status ?? ''
  const flag = f.correctness_flag ?? ''
  if (wasReplaced(f)) return 'Replaced'
  if (status.startsWith('fixed (framerate') || status.startsWith('fixed (rate')) {
    const pair = parseRatePair(status)
    return pair ? `${pair[0]} → ${pair[1]} fps` : 'Rescaled'
  }
  if (status.startsWith('fixed') || status.startsWith('would fix')) {
    const shift = f.sync_max_shift_s ?? parseDelta(status)
    const sections = parseSections(status)
      ?? (f.sync_split_blocks && f.sync_split_blocks > 1 ? f.sync_split_blocks : null)
    let s = shift !== null ? `Moved ${formatShift(shift)}` : 'Moved'
    if (sections) s += ` · ${sections} sections`
    return s
  }
  if (status === 'already in sync') return 'No change'
  if (flag === 'generated') return 'New file'
  if (status === 'missing' || flag === 'skipped') return '—'
  return 'Not changed'
}

/** "What changed" lines for the file page (and the dashboard's first line each). */
export function changeLines(f: FileRow): string[] {
  const lines: string[] = []
  const status = f.sync_status ?? ''
  if (wasReplaced(f)) {
    lines.push('Replaced with a new subtitle from Bazarr')
    if ((f.auto_action ?? '').includes('blacklist')) {
      lines.push('Old subtitle blacklisted in Bazarr')
    }
  } else if (status.startsWith('fixed (framerate') || status.startsWith('fixed (rate')) {
    const pair = parseRatePair(status)
    let s = pair ? `Rescaled ${pair[0]} → ${pair[1]} fps` : 'Rescaled to match this video'
    if (f.sync_max_shift_s !== null && f.sync_max_shift_s !== undefined) {
      s += ` (up to ${f.sync_max_shift_s.toFixed(1)} s at the end)`
    }
    lines.push(s)
  } else if (status.startsWith('fixed') || status.startsWith('would fix')) {
    const dryRun = status.startsWith('would fix')
    const shift = f.sync_max_shift_s ?? parseDelta(status)
    const sections = parseSections(status)
      ?? (f.sync_split_blocks && f.sync_split_blocks > 1 ? f.sync_split_blocks : null)
    lines.push(shift !== null
      ? `${dryRun ? 'Would move' : 'Moved'} ${formatShift(shift)}`
      : 'Moved to match the audio')
    if (sections) lines.push(`${sections} sections re-timed`)
  } else if ((f.correctness_flag ?? '') === 'generated') {
    lines.push('New subtitle created from the audio')
  }
  if ((f.line_order_fixed ?? 0) > 0) {
    lines.push(`${f.line_order_fixed} swapped line pairs put back in order`)
  }
  return lines
}

/** "Title · S01E02" for episodes, the title for movies, else the video file name. */
export function fileDisplayName(f: Pick<FileRow, 'series_or_movie_title' | 'season_episode' | 'video_path'>): string {
  const title = f.series_or_movie_title
  if (title && f.season_episode) return `${title} · ${f.season_episode}`
  if (title) return title
  const parts = f.video_path.split('/')
  return parts[parts.length - 1] || f.video_path
}

/** Verdict pill for a correctness_history row (which records no reason). */
export function historyVerdict(flag: string | null): { cls: Verdict['cls']; icon: string; label: string } {
  if (flag === 'ok') return { cls: 'pill-ok', icon: '✓', label: 'In sync' }
  if (flag === 'SUSPECT') return { cls: 'pill-bad', icon: '✕', label: 'Suspect' }
  return { cls: 'pill-muted', icon: '–', label: flag ?? '—' }
}

export interface AttentionMeta {
  label: string
  cls: Verdict['cls']
  icon: string
  todo: string
}

/** Dashboard "Needs attention" rows: /api/stats/attention reasons plus No subtitle. */
export const ATTENTION_META: Record<SuspectReason | 'other' | 'nosub', AttentionMeta> = {
  wrong_subtitle: { label: 'Wrong subtitle', cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  partly_out_of_sync: { label: 'Partly out of sync', cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  missing_lines: { label: 'Missing lines', cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  past_audio_end: { label: "Doesn't fit this video", cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  lines_out_of_order: { label: 'Lines out of order', cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  unreliable_timing: { label: 'Unreliable timing', cls: 'pill-bad', icon: '✕', todo: 'Fetch a replacement.' },
  no_speech_heard: { label: 'No speech heard', cls: 'pill-warn', icon: '!', todo: 'Check the audio track, then Re-check.' },
  check_failed: { label: 'Check failed', cls: 'pill-warn', icon: '!', todo: 'Re-check.' },
  other: { label: 'Needs attention', cls: 'pill-bad', icon: '✕', todo: 'Re-check to get a fresh verdict.' },
  nosub: { label: 'No subtitle', cls: 'pill-muted', icon: '–', todo: 'Generate one with Whisper, or wait for Bazarr.' },
}
