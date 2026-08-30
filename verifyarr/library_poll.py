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

from verifyarr import db, log
from verifyarr.discovery import build_library_video_rows, discover_all_videos, discover_missing, discover_pairs
from verifyarr.settings import Config


def refresh_library_cache(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The actual discovery work (a directory walk, no sync/correctness/API calls) — shared by
    the scheduled poll below and the manual "Detect now" button (see web/routers/library.py) so
    the two can never drift apart. Returns {"pairs", "missing"} counts for logging/reporting."""
    pairs = discover_pairs(cfg)
    all_videos = discover_all_videos(cfg)
    missing = discover_missing(cfg, pairs, all_videos)
    db.replace_library_videos(conn, build_library_video_rows(cfg, pairs, all_videos))
    for video, lang in missing:
        db.mark_missing(conn, video, lang, cfg.media_root_for(video))
    return {"pairs": len(pairs), "missing": len(missing)}


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
