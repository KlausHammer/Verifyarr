"""Runs/Activity — start/cancel a job, view history, and stream a live log via SSE. Only one
job at a time (enforced by db.create_run's partial unique index + jobs.JobRunner's lock)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from verifyarr import db, jobs
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/runs", tags=["runs"])


class StartRunBody(BaseModel):
    mode: str = "sweep"  # "sweep" | "single"
    force: bool = False
    dry_run: Optional[bool] = None
    video: Optional[str] = None
    subtitle: Optional[str] = None
    lang: Optional[str] = None
    # sweep-only scoping — see Library page's Scan/Rescan (per-title/per-season or whole list).
    kind: Optional[str] = None  # "movie" | "series"
    title: Optional[str] = None
    season: Optional[str] = None  # e.g. "S03" — series only


def _serialize(row) -> dict:
    return dict(row)


@router.get("")
def list_runs(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=200),
              user=Depends(require_auth), conn=Depends(get_conn)):
    rows, total = db.list_runs(conn, page=page, page_size=page_size)
    return {"items": [_serialize(r) for r in rows], "total": total, "page": page, "page_size": page_size,
            "current_run_id": jobs.runner.current_run_id()}


@router.post("")
def start_run(body: StartRunBody, user=Depends(require_auth), conn=Depends(get_conn)):
    try:
        if body.mode == "sweep":
            run_id = jobs.runner.start_sweep("manual_ui", force=body.force, dry_run_override=body.dry_run,
                                              kind=body.kind, title=body.title, season=body.season)
        elif body.mode == "single":
            if not body.video or not body.subtitle:
                raise HTTPException(status_code=400, detail="video and subtitle are required for mode=single")
            run_id = jobs.runner.start_single("manual_ui", Path(body.video), Path(body.subtitle),
                                               lang=body.lang, dry_run_override=body.dry_run)
        else:
            raise HTTPException(status_code=400, detail="mode must be 'sweep' or 'single'")
    except jobs.RunAlreadyActive:
        raise HTTPException(status_code=409, detail="a job is already running")
    return {"run_id": run_id}


@router.get("/{run_id}")
def get_run(run_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    row = db.get_run(conn, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize(row)


@router.get("/{run_id}/log")
def get_log(run_id: int, after_id: int = 0, user=Depends(require_auth), conn=Depends(get_conn)):
    lines = db.list_log_lines(conn, run_id, after_id=after_id)
    return {"items": [_serialize(l) for l in lines]}


@router.post("/{run_id}/cancel")
def cancel_run(run_id: int, user=Depends(require_auth), conn=Depends(get_conn)):
    if jobs.runner.current_run_id() != run_id:
        raise HTTPException(status_code=409, detail="this run is not the one currently active")
    ok = jobs.runner.cancel()
    if not ok:
        raise HTTPException(status_code=409, detail="no job is running")
    return {"ok": True}


@router.get("/{run_id}/stream")
async def stream_run(run_id: int, user=Depends(require_auth)):
    """Server-Sent Events — polled from run_log_lines every second (not pushed), so both a
    running AND an already-finished job can be streamed the same way, and a page refresh
    mid-job loses nothing: the client just restarts the stream with the last after_id."""

    async def gen():
        conn = db.connect()
        try:
            after_id = 0
            while True:
                lines = db.list_log_lines(conn, run_id, after_id=after_id)
                for line in lines:
                    after_id = line["id"]
                    payload = {"id": line["id"], "ts": line["ts"], "level": line["level"], "message": line["message"]}
                    yield f"event: log\ndata: {json.dumps(payload)}\n\n"
                run = db.get_run(conn, run_id)
                if run is None:
                    yield "event: error\ndata: {}\n\n"
                    return
                if run["status"] != "running":
                    yield f"event: done\ndata: {json.dumps(dict(run))}\n\n"
                    return
                await asyncio.sleep(1.0)
        finally:
            conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
