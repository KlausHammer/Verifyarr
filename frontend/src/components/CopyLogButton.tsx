import { useState } from 'react'

interface LogLineLike {
  ts: string
  level: string
  message: string
}

/** navigator.clipboard is only available in a "secure context" (https, or localhost) — verifyarr
 * is commonly self-hosted over plain http:// on a LAN, where it's simply undefined, so the
 * modern API silently can't be used at all. Falls back to the old execCommand('copy') dance via
 * a hidden, off-screen textarea, which still works over plain http. */
async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // fall through to the legacy fallback below
    }
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.top = '0'
    ta.style.left = '0'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

/** Copies a log view's lines as plain text (timestamp + level + message per line) -- e.g. to
 * paste into a bug report or a chat. Shows "Copy failed" briefly rather than silently doing
 * nothing if both the modern and legacy copy paths are unavailable. */
export default function CopyLogButton({ lines }: { lines: LogLineLike[] }) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle')

  async function copy() {
    const text = lines
      .map((l) => `${new Date(l.ts).toLocaleTimeString('en-US')} ${l.level} ${l.message}`)
      .join('\n')
    const ok = await copyText(text)
    setStatus(ok ? 'copied' : 'failed')
    setTimeout(() => setStatus('idle'), 1500)
  }

  return (
    <button className="btn btn-sm" type="button" disabled={lines.length === 0} onClick={copy}>
      {status === 'copied' ? 'Copied!' : status === 'failed' ? 'Copy failed' : 'Copy log'}
    </button>
  )
}
