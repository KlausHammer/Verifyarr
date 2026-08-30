"""FastAPI dependencies — DB connection per request and the login check. Each request opens
its own sqlite3 connection (see verifyarr.db.connect) instead of sharing one global, so the
API thread and any running background job thread never touch the same connection object
at once."""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException

from verifyarr import auth, db
from verifyarr.settings import Config

SESSION_COOKIE = "verifyarr_session"


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_cfg(conn=Depends(get_conn)) -> Config:
    return Config.from_db(conn)


def require_auth(verifyarr_session: str = Cookie(default=None), conn=Depends(get_conn)):
    user = auth.get_session_user(conn, verifyarr_session)
    if user is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return user
