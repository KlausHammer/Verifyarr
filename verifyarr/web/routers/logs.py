"""The app's own log — Settings -> Log's viewer. Independent of any one run (see runs.py's
per-run/SSE log for that); this is the same process log a `docker logs` would show, persisted (see
applog.py) so it survives past your terminal's scrollback and can be filtered/searched in the UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from verifyarr import db
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _serialize(row) -> dict:
    return dict(row)


@router.get("")
def list_logs(after_id: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000),
              user=Depends(require_auth), conn=Depends(get_conn)):
    rows = db.list_app_log_lines(conn, after_id=after_id, limit=limit)
    return {"items": [_serialize(r) for r in rows]}
