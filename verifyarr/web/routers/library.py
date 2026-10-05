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

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from verifyarr import db
from verifyarr.library_poll import get_progress, refresh_library_cache, request_cancel
from verifyarr.settings import Config
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/library", tags=["library"])

# season_episode is e.g. "S03E02" — the first 3 chars ("S03") identify the season.
SEASON_PREFIX_LENGTH = 3

# Placeholder season shown when a video has no parseable season_episode at all.
UNKNOWN_SEASON_LABEL = "Unknown"


def new_group_bucket(**extra) -> dict:
    """A fresh per-title (or per-season) counter dict — every count starts at zero so
    accumulation below can blindly += without checking for missing keys first."""
    return {
        "video_count": 0, "subtitle_detected_count": 0, "processed_count": 0,
        "ok_count": 0, "suspect_count": 0, "missing_count": 0, "last_processed": None,
        "bazarr_matched": False,  # True as soon as ANY video in the bucket matched
        **extra,
    }


def _track_latest_timestamp(bucket: dict, timestamp) -> None:
    """Keep the newest last_processed seen so far — the group's "last activity" shown
    in the UI. None means nothing in the bucket has ever been processed."""
    if timestamp and (bucket["last_processed"] is None or timestamp > bucket["last_processed"]):
        bucket["last_processed"] = timestamp


def accumulate_video_into_bucket(bucket: dict, video: dict, file_rows: list) -> None:
    """Fold one library video (plus its files-table rows) into a title or season bucket."""
    bucket["video_count"] += 1
    if video["has_subtitle"]:
        bucket["subtitle_detected_count"] += 1
    if video["bazarr_matched"]:
        bucket["bazarr_matched"] = True
    if any(row["last_processed"] for row in file_rows):
        bucket["processed_count"] += 1
    for row in file_rows:
        # SUSPECT and unknown both need attention (same population the Suspect link filters).
        if row["correctness_flag"] in ("SUSPECT", "unknown"):
            bucket["suspect_count"] += 1
        elif row["correctness_flag"] == "ok":
            bucket["ok_count"] += 1
        if row["sync_status"] == "missing":
            bucket["missing_count"] += 1
        _track_latest_timestamp(bucket, row["last_processed"])


def load_file_rows_by_video(conn) -> dict:
    """All files-table rows grouped by video_path — one query for the whole response,
    so grouping N videos costs one round trip instead of one query per video."""
    rows_by_video: dict = {}
    for row in conn.execute(
        "SELECT video_path, sync_status, correctness_flag, last_processed FROM files"
    ).fetchall():
        rows_by_video.setdefault(row["video_path"], []).append(row)
    return rows_by_video


def season_of(video: dict) -> str:
    """The "S03" season key for a series video, or a placeholder when unparseable."""
    return (video["season_episode"] or "")[:SEASON_PREFIX_LENGTH] or UNKNOWN_SEASON_LABEL


def sorted_groups(groups: dict, kind: Optional[str]) -> list:
    """Groups alphabetically by title (case-insensitive), with each series group's
    seasons sorted chronologically — the order the sidebar renders."""
    items = sorted(groups.values(), key=lambda group: group["title"].lower())
    if kind == "series":
        for group in items:
            group["seasons"] = sorted(group["seasons"].values(), key=lambda season: season["season"])
    return items


def grouped_response(conn, kind: Optional[str]) -> dict:
    rows_by_video = load_file_rows_by_video(conn)

    groups: dict = {}
    for video in db.list_library_videos(conn, kind=kind):
        # "Billions" and "BILLIONS" (Bazarr's title vs a release-folder name) are one title.
        group = groups.setdefault(video["title"].casefold(), new_group_bucket(
            title=video["title"], seasons={} if kind == "series" else None,
        ))
        if group["title"].isupper() and not video["title"].isupper():
            group["title"] = video["title"]
        video_rows = rows_by_video.get(video["video_path"], [])
        accumulate_video_into_bucket(group, video, video_rows)

        # Season breakdown (series only, see Series page's expand/collapse + per-season Scan).
        if kind == "series":
            season_key = season_of(video)
            season_bucket = group["seasons"].setdefault(
                season_key, new_group_bucket(season=season_key))
            accumulate_video_into_bucket(season_bucket, video, video_rows)

    return {
        "items": sorted_groups(groups, kind),
        "total": len(groups),
        "last_scanned_at": db.get_setting_raw(conn, "library.last_scanned_at"),
    }


@router.get("")
def list_library(kind: Optional[str] = Query(None, pattern="^(movie|series)$"),
                 user=Depends(require_auth), conn=Depends(get_conn)):
    response = grouped_response(conn, kind)
    cfg = Config.from_db(conn)
    if cfg.bazarr_url and cfg.bazarr_api_key:
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bazarr_matched), 0) AS m FROM library_videos").fetchone()
        response["bazarr_match"] = {"videos": row["n"], "matched": row["m"]}
    return response


_EPISODE_NUM_RE = re.compile(r"E(\d+)", re.IGNORECASE)


def _episode_sort_key(video: dict) -> tuple:
    """Episode 1 first. Videos with no SxxEyy (extras) come last, by file name."""
    se = video["season_episode"] or ""
    m = _EPISODE_NUM_RE.search(se)
    return (0, int(m.group(1)), "") if m else (1, 0, Path(video["video_path"]).name.lower())


@router.get("/series/episodes")
def series_episodes(title: str = Query(..., min_length=1), user=Depends(require_auth), conn=Depends(get_conn)):
    """Every season and episode of one series, episodes in order, with each subtitle file's status
    (the series page's drill-down). Title matches case-insensitively, like the library list."""
    files_by_video: dict = {}
    for r in conn.execute("SELECT id, video_path, subtitle_path, lang, sync_status, correctness_flag, reason, "
                          "last_processed FROM files WHERE subtitle_path IS NOT NULL").fetchall():
        files_by_video.setdefault(r["video_path"], []).append(dict(r))
    seasons: dict = {}
    for video in db.list_library_videos(conn, kind="series"):
        if video["title"].casefold() != title.casefold():
            continue
        seasons.setdefault(season_of(video), []).append(video)
    if not seasons:
        raise HTTPException(status_code=404, detail="series not found")
    out = []
    for season, videos in sorted(seasons.items()):
        out.append({"season": season, "episodes": [
            {"video_path": v["video_path"], "name": Path(v["video_path"]).name,
             "season_episode": v["season_episode"], "has_subtitle": bool(v["has_subtitle"]),
             "embedded_langs": json.loads(v["embedded_langs_json"] or "[]"),
             "subtitles": files_by_video.get(v["video_path"], [])}
            for v in sorted(videos, key=_episode_sort_key)]})
    return {"title": title, "seasons": out}


@router.get("/rescan/status")
def rescan_status(user=Depends(require_auth)):
    """Polled by the "Detect now" button to show a live X/Y counter, and to recover the right
    "still running" state after switching Settings tabs and back (see get_progress's docstring
    for why this lives server-side rather than in the button's own component state)."""
    return get_progress()


@router.post("/rescan/cancel")
def cancel_rescan(user=Depends(require_auth)):
    """The Stop button that replaces "Detect now" while a rescan is running. Cooperative --
    stops the concurrent embedded-subtitle check between videos, doesn't kill an in-progress
    ffprobe call, and the rescan's result is discarded entirely rather than partially saved
    (see library_poll.refresh_library_cache)."""
    request_cancel()
    return get_progress()


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
    response = grouped_response(conn, kind)
    response["cancelled"] = result.get("cancelled", False)
    if not response["cancelled"]:
        response["pairs_found"] = result["pairs"]
        response["missing_found"] = result["missing"]
    return response
