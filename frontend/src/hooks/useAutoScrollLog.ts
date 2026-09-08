import { useEffect, useRef } from 'react'

// Keeps a scrolling log box pinned to the bottom as new lines arrive -- but stops doing that the
// moment the user scrolls up to read something older, so a live-updating log doesn't yank them
// back down mid-read. Checking "is the user at the bottom" AFTER lines are appended doesn't work
// (the box has already grown to fit the new content by then), so onScroll tracks the user's own
// scrolling continuously instead, and the auto-scroll effect just trusts whatever it last saw.
const BOTTOM_THRESHOLD_PX = 40

export function useAutoScrollLog(lines: unknown[]) {
  const ref = useRef<HTMLDivElement>(null)
  const stickToBottom = useRef(true)

  function onScroll() {
    const el = ref.current
    if (!el) return
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD_PX
  }

  useEffect(() => {
    // An empty log (a fresh run just started, e.g.) always re-pins -- there's nothing above to
    // have been reading, so there's no reason to make the user re-opt-in to auto-scroll.
    if (lines.length === 0) stickToBottom.current = true
    if (stickToBottom.current && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight
    }
  }, [lines])

  return { ref, onScroll }
}
