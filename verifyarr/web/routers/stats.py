"""Statistik — match-rate over tid (Stats-siden) og en samlet status-oversigt (Dashboard)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from verifyarr import db
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary")
def summary(user=Depends(require_auth), conn=Depends(get_conn)):
    return db.summary_stats(conn)


@router.get("/match-rate")
def match_rate(group_by: str = Query("day", pattern="^(day|week)$"), days: int = Query(90, ge=1, le=730),
                user=Depends(require_auth), conn=Depends(get_conn)):
    rows = db.match_rate_series(conn, group_by=group_by, days=days)
    return {"items": [dict(r) for r in rows]}
