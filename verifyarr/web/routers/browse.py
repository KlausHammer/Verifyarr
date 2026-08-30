"""Filesystem browser — used by Settings -> General to pick Root Folders by clicking through
the REAL folder structure inside the container (i.e. what Docker actually mounted via
docker-compose.yml's volumes), instead of typing container paths blind. Same UX as
Sonarr/Radarr/Bazarr's own "Add Root Folder" browser."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from verifyarr.web.deps import require_auth

router = APIRouter(prefix="/api/browse", tags=["browse"])


@router.get("")
def browse(path: str = Query("/"), user=Depends(require_auth)):
    p = Path(path or "/").resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"path does not exist: {p}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {p}")
    try:
        raw_children = list(p.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"permission denied: {p}")
    # Stat individual entries defensively — on Windows drives mounted via WSL/DrvFs, locked
    # system files (pagefile.sys, hiberfil.sys, ...) raise PermissionError on stat() even though
    # the directory listing itself succeeded. One unreadable entry shouldn't fail the whole
    # browse, so entries we can't stat are silently skipped rather than aborting the request.
    children = []
    for c in raw_children:
        if c.name.startswith("."):
            continue
        try:
            if c.is_dir():
                children.append(c)
        except (PermissionError, OSError):
            continue
    children.sort(key=lambda c: c.name.lower())
    return {
        "path": str(p),
        "parent": str(p.parent) if str(p.parent) != str(p) else None,
        "entries": [{"name": c.name, "path": str(c)} for c in children],
    }


@router.get("/check")
def check_path(path: str = Query(...), user=Depends(require_auth)):
    """Used to show a small status icon next to each already-configured Root Folder — does
    the path exist right now, and how much is in it (a quick health check without having to
    open the browser again)."""
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return {"exists": False, "entry_count": None}
    try:
        entry_count = sum(1 for _ in p.iterdir())
    except PermissionError:
        return {"exists": True, "entry_count": None}
    return {"exists": True, "entry_count": entry_count}
