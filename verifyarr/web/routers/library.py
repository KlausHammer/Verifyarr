"""Library overview — grouped per series/movie (like Bazarr's Series/Movies list), unlike
files.py's flat file-by-file list. Reads from the library_videos CACHE (see db.py), NEVER a
live filesystem scan per page load — that's too expensive to do per GET, especially over
slow network/WSL mounts. The cache is filled by either a sweep (which already scans the tree
anyway, see jobs._run_sweep), library_poll.py's periodic background check
(scheduling.poll_library_enabled), or the manual POST /rescan below (the "Detect now" button
in Settings -> General) — all three call the same library_poll.refresh_library_cache, so
they can never drift out of sync with each other.

`kind` (movie/series, see Config.kind_for) is cached PER video, so Movies and Series can be
shown as two separate sidebar pages (like Radarr/Sonarr are two separate apps) without
another scan per page — same cache, filtered differently."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from verifyarr import db
from verifyarr.library_poll import refresh_library_cache
from verifyarr.settings import Config
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/library", tags=["library"])


def _new_bucket(**extra) -> dict:
    return {
        "video_count": 0, "subtitle_detected_count": 0, "processed_count": 0,
        "ok_count": 0, "suspect_count": 0, "missing_count": 0, "last_processed": None,
        **extra,
    }


def _bump(bucket: dict, v, video_rows: list) -> None:
    bucket["video_count"] += 1
    if v["has_subtitle"]:
        bucket["subtitle_detected_count"] += 1
    if any(r["last_processed"] for r in video_rows):
        bucket["processed_count"] += 1
    for r in video_rows:
        if r["correctness_flag"] == "SUSPECT":
            bucket["suspect_count"] += 1
        elif r["correctness_flag"] == "ok":
            bucket["ok_count"] += 1
        if r["sync_status"] == "missing":
            bucket["missing_count"] += 1
        if r["last_processed"] and (bucket["last_processed"] is None or r["last_processed"] > bucket["last_processed"]):
            bucket["last_processed"] = r["last_processed"]


def _grouped_response(conn, kind: Optional[str]) -> dict:
    rows_by_video: dict = {}
    for row in conn.execute(
        "SELECT video_path, sync_status, correctness_flag, last_processed FROM files"
    ).fetchall():
        rows_by_video.setdefault(row["video_path"], []).append(row)

    groups: dict = {}
    for v in db.list_library_videos(conn, kind=kind):
        g = groups.setdefault(v["title"], _new_bucket(
            title=v["title"], seasons={} if kind == "series" else None,
        ))
        video_rows = rows_by_video.get(v["video_path"], [])
        _bump(g, v, video_rows)

        # Season breakdown (series only, see Series page's expand/collapse + per-season Scan) —
        # season_episode is e.g. "S03E02", first 3 chars ("S03") is the season.
        if kind == "series":
            season = (v["season_episode"] or "")[:3] or "Unknown"
            sg = g["seasons"].setdefault(season, _new_bucket(season=season))
            _bump(sg, v, video_rows)

    items = sorted(groups.values(), key=lambda g: g["title"].lower())
    for g in items:
        if g["seasons"] is not None:
            g["seasons"] = sorted(g["seasons"].values(), key=lambda s: s["season"])
    return {
        "items": items,
        "total": len(items),
        "last_scanned_at": db.get_setting_raw(conn, "library.last_scanned_at"),
    }


@router.get("")
def list_library(kind: Optional[str] = Query(None, pattern="^(movie|series)$"),
                  user=Depends(require_auth), conn=Depends(get_conn)):
    return _grouped_response(conn, kind)


@router.post("/rescan")
def rescan_library(kind: Optional[str] = Query(None, pattern="^(movie|series)$"),
                    user=Depends(require_auth), conn=Depends(get_conn)):
    """Fast on-demand cache refresh — discovery only (folder walk), NO sync/correctness
    processing, so it's fine to click right after adding new files without waiting for/
    triggering a full sweep. This is the "Detect now" button in Settings -> General. Always
    scans BOTH folders (Movies + Series are one table), `kind` only controls which filtered
    result is returned — the Movies page and Series page both call this endpoint, each with
    its own filter, and in effect refresh each other's cache too as a bonus."""
    cfg = Config.from_db(conn)
    result = refresh_library_cache(conn, cfg)
    response = _grouped_response(conn, kind)
    response["pairs_found"] = result["pairs"]
    response["missing_found"] = result["missing"]
    return response
