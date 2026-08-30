"""Bazarr blacklist overview — our own log of what verifyarr has gotten Bazarr to blacklist
(see db.blacklist_actions, populated from pipeline.handle_suspect/bazarr.remediate_suspect)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from verifyarr import db
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/bazarr", tags=["bazarr"])


@router.get("/blacklist")
def list_blacklist(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                    user=Depends(require_auth), conn=Depends(get_conn)):
    rows, total = db.list_blacklist_actions(conn, page=page, page_size=page_size)
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}
