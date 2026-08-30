"""Periodic, discovery-only refresh of the Library page's cache (scheduling.poll_library_enabled,
on by default every scheduling.poll_library_interval_minutes) — so newly added media shows up
under Movies/Series without waiting for the next scheduled sweep or a manual Scan/Rescan click.

Deliberately does NOT run sync/correctness/line-order (no alass, no API calls) — it's the exact
same discovery pass _run_sweep already does at the start of a sweep (see jobs.py), just without
the per-file processing loop, so it's cheap enough to run often. Separate from bazarr_poll.py's
poll_wanted_subtitles, which watches Bazarr's OWN wanted-lists, not the filesystem — the two
settings are deliberately named differently (poll_library_* here vs poll_new_media_* there) to
keep them from being confused with each other."""

from __future__ import annotations

import sqlite3
import threading

from verifyarr import db, log
from verifyarr.discovery import build_library_video_rows, discover_all_videos, discover_missing, discover_pairs
from verifyarr.settings import Config

# Progress for whichever refresh_library_cache() call is currently running -- read by
# GET /api/library/rescan/status so the "Detect now" button (Settings -> General) can show a
# live counter, and still show the right state after a settings-tab switch unmounts and
# remounts the component (its own local React state doesn't survive that, this does).
# Deliberately module-level/shared rather than per-request: there's only ever one discovery
# pass worth watching at a time, whether it was triggered by the button or the periodic poll.
_progress_lock = threading.Lock()
_progress = {"running": False, "done": 0, "total": 0}


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def refresh_library_cache(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The actual discovery work (a directory walk, no sync/correctness/API calls) — shared by
    the scheduled poll below and the manual "Detect now" button (see web/routers/library.py) so
    the two can never drift apart. Returns {"pairs", "missing"} counts for logging/reporting."""
    # Marked running before the directory walk (discover_pairs/discover_all_videos) even starts —
    # on a large library over a slow/network-mounted media folder, the walk itself can take a
    # while, and total is still unknown at that point (shown as 0/0, i.e. "still scanning
    # folders"). Without this, GET .../status would report running=False for that whole phase
    # and the button would look inert instead of just not-yet-counting.
    with _progress_lock:
        _progress.update(running=True, done=0, total=0)

    pairs = discover_pairs(cfg)
    all_videos = discover_all_videos(cfg)
    # embedded_cache is shared across both passes below so a video with zero external subtitles
    # (the only case either function ffprobes) isn't probed twice -- each ffprobe call is a real
    # filesystem read, which matters on slow/network-mounted media.
    embedded_cache: dict = {}
    # *2: discover_missing and build_library_video_rows each do their own pass over all_videos
    # (the only genuinely slow part of either is the embedded-subtitle ffprobe call some videos
    # need) -- one running counter across both keeps the reported progress simple and always
    # increasing, rather than restarting partway through.
    total = len(all_videos) * 2
    done = 0
    with _progress_lock:
        _progress.update(done=0, total=total)

    def _bump() -> None:
        nonlocal done
        done += 1
        with _progress_lock:
            _progress["done"] = done

    try:
        missing = discover_missing(cfg, pairs, all_videos, progress_cb=_bump, embedded_cache=embedded_cache)
        rows = build_library_video_rows(cfg, pairs, all_videos, progress_cb=_bump, embedded_cache=embedded_cache)
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
        log.info("Library poll: refreshed cache — %d video/subtitle pair(s), %d missing language(s)",
                  result["pairs"], result["missing"])
    except Exception as e:
        log.warning("Library poll failed: %s", e)
    finally:
        conn.close()
