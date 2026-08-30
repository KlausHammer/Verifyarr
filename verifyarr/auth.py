"""Login — one shared admin password, session via httponly cookie against the `sessions`
table (no JWT/secret needed). Passwords are hashed with scrypt (stdlib `hashlib`).

First visit: `users` table is empty -> the webapp forces a "create password" screen
(GET /api/auth/status returns needs_setup=true) instead of a secret baked into
docker-compose.yml. Forgotten password: `python3 verifyarr.py reset-password` deletes the
users row so the setup screen reappears — no web access needed for that.

MIN_PASSWORD_LENGTH is deliberately low — this is a light barrier on a private home
network, not a hardened login system."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

SESSION_TTL_DAYS = 30
MIN_PASSWORD_LENGTH = 5
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p), dklen=32)
        return secrets.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def has_user(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(conn, username: str, password: str) -> None:
    if has_user(conn):
        raise ValueError("a user already exists")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at, password_updated_at) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), now, now),
    )
    conn.commit()


def get_user_by_username(conn, username: str):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def change_password(conn, user_id: int, new_password: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE users SET password_hash = ?, password_updated_at = ? WHERE id = ?",
        (hash_password(new_password), now, user_id),
    )
    # All existing sessions are invalidated on a password change, including the current
    # one — the client re-logs-in right after (see routers/auth.py).
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def create_session(conn, user_id: int, user_agent: Optional[str] = None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, now.isoformat(), (now + timedelta(days=SESSION_TTL_DAYS)).isoformat(), user_agent),
    )
    conn.commit()
    return token


def get_session_user(conn, token: Optional[str]):
    if not token:
        return None
    row = conn.execute("""
        SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ? AND sessions.expires_at > ?
    """, (token, datetime.now(timezone.utc).isoformat())).fetchone()
    return row


def delete_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def reset_all_users(conn) -> None:
    """Used by `verifyarr.py reset-password` — deletes ALL users+sessions so the setup
    screen (create new password) shows again on next visit."""
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM users")
    conn.commit()
