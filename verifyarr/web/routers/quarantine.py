"""Quarantine/backup browser with a restore button — see fileops.py for the actual move/copy
logic. On restore, the frontend must specify which Root Folder (media_root) the file
belonged to if more than one is configured — that can't be derived anymore once only the
RELATIVE path was saved (see fileops.backup_subtitle/quarantine_subtitle)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from verifyarr import fileops
from verifyarr.settings import Config
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(tags=["quarantine"])


class RestoreBody(BaseModel):
    path: str
    media_root: Optional[str] = None


def _resolve_media_root(cfg: Config, override: Optional[str]) -> Path:
    if override:
        return Path(override)
    if len(cfg.media_roots) == 1:
        return cfg.media_roots[0]
    raise HTTPException(status_code=400, detail="multiple Root Folders configured — specify media_root explicitly")


@router.get("/api/quarantine")
def list_quarantine(user=Depends(require_auth), conn=Depends(get_conn)):
    cfg = Config.from_db(conn)
    return {"items": fileops.list_archived(cfg.quarantine_dir, is_backup=False),
            "media_roots": [str(r) for r in cfg.media_roots]}


@router.post("/api/quarantine/restore")
def restore_quarantine(body: RestoreBody, user=Depends(require_auth), conn=Depends(get_conn)):
    cfg = Config.from_db(conn)
    media_root = _resolve_media_root(cfg, body.media_root)
    try:
        target = fileops.restore_from_quarantine(body.path, cfg.quarantine_dir, media_root)
    except (FileNotFoundError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "restored_to": str(target)}


@router.get("/api/backups")
def list_backups(user=Depends(require_auth), conn=Depends(get_conn)):
    cfg = Config.from_db(conn)
    return {"items": fileops.list_archived(cfg.backup_dir, is_backup=True),
            "media_roots": [str(r) for r in cfg.media_roots]}


@router.post("/api/backups/restore")
def restore_backup(body: RestoreBody, user=Depends(require_auth), conn=Depends(get_conn)):
    cfg = Config.from_db(conn)
    media_root = _resolve_media_root(cfg, body.media_root)
    try:
        target = fileops.restore_from_backup(body.path, cfg.backup_dir, media_root)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "restored_to": str(target)}
