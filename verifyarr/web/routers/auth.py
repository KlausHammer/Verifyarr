"""Login — see verifyarr.auth for the actual hash/session logic. docker-compose.yml assumes
no password; first visit shows the setup screen (needs_setup=true below)."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from verifyarr import auth
from verifyarr.web.deps import SESSION_COOKIE, get_conn, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupBody(BaseModel):
    username: str = "admin"
    password: str


class LoginBody(BaseModel):
    username: str = "admin"
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                         max_age=auth.SESSION_TTL_DAYS * 86400, path="/")


@router.get("/status")
def status(verifyarr_session: str = Cookie(default=None), conn=Depends(get_conn)):
    needs_setup = not auth.has_user(conn)
    user = auth.get_session_user(conn, verifyarr_session)
    return {"needs_setup": needs_setup, "authenticated": user is not None,
            "username": user["username"] if user else None}


@router.post("/setup")
def setup(body: SetupBody, response: Response, request: Request, conn=Depends(get_conn)):
    if auth.has_user(conn):
        raise HTTPException(status_code=409, detail="a user already exists — use /login")
    if len(body.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
    auth.create_user(conn, body.username, body.password)
    user = auth.get_user_by_username(conn, body.username)
    token = auth.create_session(conn, user["id"], request.headers.get("user-agent"))
    _set_session_cookie(response, token)
    return {"ok": True}


@router.post("/login")
def login(body: LoginBody, response: Response, request: Request, conn=Depends(get_conn)):
    user = auth.get_user_by_username(conn, body.username)
    if user is None or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="incorrect username or password")
    token = auth.create_session(conn, user["id"], request.headers.get("user-agent"))
    _set_session_cookie(response, token)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, verifyarr_session: str = Cookie(default=None), conn=Depends(get_conn)):
    if verifyarr_session:
        auth.delete_session(conn, verifyarr_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, response: Response, request: Request,
                     user=Depends(require_auth), conn=Depends(get_conn)):
    if not auth.verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    if len(body.new_password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"new password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
    auth.change_password(conn, user["id"], body.new_password)
    # change_password deletes ALL sessions (including the current one) — create a new one
    # right away so the client isn't kicked out by its own action.
    token = auth.create_session(conn, user["id"], request.headers.get("user-agent"))
    _set_session_cookie(response, token)
    return {"ok": True}
