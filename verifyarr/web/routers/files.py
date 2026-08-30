"""Files — processed files, including ones that are 'missing' (sync_status='missing', see
discovery.discover_missing/db.mark_missing)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from verifyarr import db, jobs
from verifyarr.bazarr import bazarr_build_history_index, bazarr_map_path
from verifyarr.pipeline import handle_suspect
from verifyarr.settings import Config
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/files", tags=["files"])


def _serialize(row) -> dict:
    return dict(row)


@router.get("")
def list_files(q: Optional[str] = None, flag: Optional[str] = None, status: Optional[str] = None,
               lang: Optional[str] = None, sort: str = "-last_processed",
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=5000),
               user=Depends(require_auth), conn=Depends(get_conn)):
    rows, total = db.list_files(conn, q=q, flag=flag, status=status, lang=lang, sort=sort,
                                 page=page, page_size=page_size)
    return {"items": [_serialize(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{file_id}")
def get_file(file_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    row = db.get_file(conn, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")
    history = conn.execute(
        "SELECT * FROM correctness_history WHERE subtitle_path = ? ORDER BY checked_at DESC LIMIT 20",
        (row["subtitle_path"],),
    ).fetchall() if row["subtitle_path"] else []
    return {"file": _serialize(row), "correctness_history": [_serialize(r) for r in history]}


@router.post("/{file_id}/run-single")
def run_single_for_file(file_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    row = db.get_file(conn, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")
    if not row["subtitle_path"]:
        raise HTTPException(status_code=400, detail="no subtitle file to process (status 'missing')")
    try:
        run_id = jobs.runner.start_single("manual_ui", Path(row["video_path"]), Path(row["subtitle_path"]),
                                           lang=row["lang"])
    except jobs.RunAlreadyActive:
        raise HTTPException(status_code=409, detail="a job is already running")
    return {"run_id": run_id}


def _apply_action(conn, file_id: int, action: str) -> dict:
    """Shared by the three manual action buttons below — lets a dry-run sweep flag SUSPECT
    files first, then the user picks per-file what to do, instead of the saved correctness/
    line-order auto_action settings. `action` is passed straight into handle_suspect, overriding
    whichever of those two settings would otherwise apply for this one call."""
    row = db.get_file(conn, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")
    if not row["subtitle_path"]:
        raise HTTPException(status_code=400, detail=f"no subtitle file to {action}")
    if jobs.runner.is_running():
        raise HTTPException(status_code=409, detail="a job is already running — wait for it to finish")

    cfg = Config.from_db(conn)
    cfg = dataclasses.replace(cfg, dry_run=False)
    subtitle_path, video_path = Path(row["subtitle_path"]), Path(row["video_path"])
    media_root = cfg.media_root_for(subtitle_path)

    history_index = None
    if action in ("blacklist", "remediate"):
        history_index = bazarr_build_history_index(cfg)
        if history_index.get(bazarr_map_path(cfg, subtitle_path)) is None:
            raise HTTPException(status_code=400,
                                 detail="no Bazarr match found for this file (does it exist in Bazarr's history?)")

    from verifyarr.discovery import target_label
    run_id = db.create_run(conn, "manual_ui", "single", cfg.dry_run, False,
                            target_kind=cfg.kind_for(video_path), target_title=target_label(video_path))
    try:
        result = handle_suspect(subtitle_path, video_path, cfg, media_root, row["lang"], None,
                                 history_index, action, conn=conn, run_id=run_id)
        db.finish_run(conn, run_id, "completed")
    except Exception as e:
        db.finish_run(conn, run_id, "failed", str(e))
        raise HTTPException(status_code=500, detail=str(e))
    return {"run_id": run_id, "result": result}


@router.post("/{file_id}/remediate")
def remediate_file(file_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    return _apply_action(conn, file_id, "remediate")


@router.post("/{file_id}/blacklist")
def blacklist_file(file_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    return _apply_action(conn, file_id, "blacklist")


@router.post("/{file_id}/quarantine")
def quarantine_file(file_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    return _apply_action(conn, file_id, "quarantine")
