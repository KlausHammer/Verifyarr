"""Statistics — match rate over time (the Stats page) and an overall status summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from verifyarr import db
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Bounds for the match-rate history window (days) — enforced by the query validator
# so a client can't request an unbounded series scan over the whole files table.
MATCH_RATE_MIN_DAYS = 1
MATCH_RATE_MAX_DAYS = 730
MATCH_RATE_DEFAULT_DAYS = 90


@router.get("/summary")
def summary(user=Depends(require_auth), conn=Depends(get_conn)):
    return db.summary_stats(conn)


@router.get("/attention")
def attention(user=Depends(require_auth), conn=Depends(get_conn)):
    """Flagged files per reason, largest first -- the dashboard's "Needs attention"."""
    return {"items": [{"reason": k, "count": v} for k, v in db.attention_counts(conn).items()]}


@router.get("/match-rate")
def match_rate(group_by: str = Query("day", pattern="^(day|week)$"),
               days: int = Query(MATCH_RATE_DEFAULT_DAYS, ge=MATCH_RATE_MIN_DAYS, le=MATCH_RATE_MAX_DAYS),
               user=Depends(require_auth), conn=Depends(get_conn)):
    rows = db.match_rate_series(conn, group_by=group_by, days=days)
    return {"items": [dict(r) for r in rows]}
