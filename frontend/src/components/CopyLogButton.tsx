import { useState } from 'react'

interface LogLineLike {
  ts: string
  level: string
  message: string
}

/** Copies a log view's lines as plain text (timestamp + level + message per line) -- e.g. to
 * paste into a bug report or a chat. Silently no-ops if the Clipboard API is unavailable
 * (non-HTTPS context, permissions) rather than throwing an error banner over something minor. */
export default function CopyLogButton({ lines }: { lines: LogLineLike[] }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    const text = lines
      .map((l) => `${new Date(l.ts).toLocaleTimeString('en-US')} ${l.level} ${l.message}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard access denied/unavailable -- nothing useful to show the user, just no-op
    }
  }

  return (
    <button className="btn btn-sm" type="button" disabled={lines.length === 0} onClick={copy}>
      {copied ? 'Copied!' : 'Copy log'}
    </button>
  )
}
