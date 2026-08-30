"""Periodic, discovery-only refresh of the Library page's cache (scheduling.poll_library_enabled,
on by default every scheduling.poll_library_interval_minutes) — so newly added media shows up
under Movies/Series without waiting for the next scheduled sweep or a manual Scan/Rescan click.

Deliberately does NOT run sync/correctness/line-order (no alass, no API calls except the one
bulk Bazarr read below) — it's the exact same discovery pass _run_sweep already does at the
start of a sweep (see jobs.py), just without the per-file processing loop, so it's cheap enough
to run often. Separate from bazarr_poll.py's poll_wanted_subtitles, which watches Bazarr's OWN
wanted-lists, not the filesystem — the two settings are deliberately named differently
(poll_library_* here vs poll_new_media_* there) to keep them from being confused with each
other."""

from __future__ import annotations

import sqlite3
import threading

from verifyarr import db, log
from verifyarr.discovery import (
    build_library_video_rows,
    discover_all_videos,
    discover_missing,
    discover_pairs,
    resolve_embedded_cache,
)
from verifyarr.settings import Config

# Progress/cancellation for whichever refresh_library_cache() call is currently running -- read
# by GET /api/library/rescan/status so the "Detect now" button (Settings -> General) can show a
# live counter, and still show the right state after a settings-tab switch unmounts and
# remounts the component (its own local React state doesn't survive that, this does).
# Deliberately module-level/shared rather than per-request: there's only ever one discovery
# pass worth watching (or cancelling) at a time, whether it was triggered by the button or the
# periodic poll.
_progress_lock = threading.Lock()
_progress = {"running": False, "done": 0, "total": 0, "cancelled": False}
_cancel_event = threading.Event()


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def request_cancel() -> None:
    """Called by POST /api/library/rescan/cancel (the Stop button). Cooperative, same
    convention as a sweep's cancel_event (see jobs.py) -- stops the concurrent ffprobe phase
    between videos, not mid-subprocess-call, and discards the in-progress rescan entirely
    rather than persisting a partial result (see refresh_library_cache)."""
    _cancel_event.set()


def refresh_library_cache(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The actual discovery work (a directory walk, no sync/correctness processing) — shared by
    the scheduled poll below and the manual "Detect now" button (see web/routers/library.py) so
    the two can never drift apart. Returns {"pairs", "missing", "cancelled"?} for logging/
    reporting.

    Embedded-subtitle language info comes from discovery.resolve_embedded_cache — Bazarr's own
    already-known embedded tracks first (one bulk read, no filesystem access at all), then a
    thread-pooled ffprobe fallback for whatever Bazarr doesn't already cover. Same helper
    jobs._run_sweep uses, so both stay equally fast and equally cancellable."""
    _cancel_event.clear()
    # Marked running before the directory walk (discover_pairs/discover_all_videos) even starts —
    # on a large library over a slow/network-mounted media folder, the walk itself can take a
    # while, and total is still unknown at that point (shown as 0/0, i.e. "still scanning
    # folders"). Without this, GET .../status would report running=False for that whole phase
    # and the button would look inert instead of just not-yet-counting.
    with _progress_lock:
        _progress.update(running=True, done=0, total=0, cancelled=False)

    pairs = discover_pairs(cfg)
    all_videos = discover_all_videos(cfg)

    def _report(done: int, total: int) -> None:
        with _progress_lock:
            _progress.update(done=done, total=total)

    embedded_cache = resolve_embedded_cache(cfg, pairs, all_videos, cancel_event=_cancel_event,
                                             progress_cb=_report)

    if _cancel_event.is_set():
        log.info("Library rescan cancelled by user")
        with _progress_lock:
            _progress.update(running=False, cancelled=True)
        return {"pairs": len(pairs), "missing": 0, "cancelled": True}

    try:
        missing = discover_missing(cfg, pairs, all_videos, embedded_cache=embedded_cache)
        rows = build_library_video_rows(cfg, pairs, all_videos, embedded_cache=embedded_cache)
        db.replace_library_videos(conn, rows)
        for video, lang in missing:
            db.mark_missing(conn, video, lang, cfg.media_root_for(video))
        return {"pairs": len(pairs), "missing": len(missing)}
    finally:
        with _progress_lock:
            _progress["running"] = False


def poll_library_for_new_media() -> None:
    """Called on a fixed interval by scheduler.py. No-ops quietly if the setting is off — this
    runs regardless of whether anyone uses the feature."""
    conn = db.connect()
    try:
        cfg = Config.from_db(conn)
        if not cfg.poll_library_enabled:
            return
        result = refresh_library_cache(conn, cfg)
        if result.get("cancelled"):
            return
        log.info("Library poll: refreshed cache — %d video/subtitle pair(s), %d missing language(s)",
                  result["pairs"], result["missing"])
    except Exception as e:
        log.warning("Library poll failed: %s", e)
    finally:
        conn.close()
