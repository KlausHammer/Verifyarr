"""Settings — all app configuration lives here instead of docker-compose.yml, so compose
only holds real Docker requirements."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from verifyarr import settings as settings_mod
from verifyarr import scheduler
from verifyarr.bazarr import bazarr_request
from verifyarr.settings import Config, normalize_url
from verifyarr.web.deps import get_conn, require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsGroupBody(BaseModel):
    values: dict[str, Any]


class TestBazarrConnectionBody(BaseModel):
    # Both optional: set = test this (not yet saved) value; omitted = use the one already
    # saved. This lets "Test connection" work BEFORE hitting Save.
    url: Optional[str] = None
    api_key: Optional[str] = None


@router.get("")
def get_all(user=Depends(require_auth), conn=Depends(get_conn)):
    return {group: settings_mod.get_settings_group(conn, group) for group in settings_mod.GROUPS}


@router.get("/{group}")
def get_group(group: str, user=Depends(require_auth), conn=Depends(get_conn)):
    try:
        return settings_mod.get_settings_group(conn, group)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown settings group: {group}")


@router.put("/{group}")
def put_group(group: str, body: SettingsGroupBody, user=Depends(require_auth), conn=Depends(get_conn)):
    if group not in settings_mod.GROUPS:
        raise HTTPException(status_code=404, detail=f"unknown settings group: {group}")
    settings_mod.set_settings_group(conn, group, body.values)
    if group == "scheduling":
        scheduler.reschedule()
    elif group == "log":
        # Takes effect immediately, no restart -- see verifyarr/__init__.py for the process-start
        # default and web/app.py's lifespan for the same call at startup.
        logging.getLogger("verifyarr").setLevel(Config.from_db(conn).log_level)
    return settings_mod.get_settings_group(conn, group)


@router.post("/bazarr/test-connection")
def test_bazarr_connection(body: TestBazarrConnectionBody = TestBazarrConnectionBody(),
                            user=Depends(require_auth), conn=Depends(get_conn)):
    cfg = Config.from_db(conn)
    # Override with what the user currently has in the form, without saving it first —
    # otherwise "Test connection" only works AFTER hitting Save, which is backwards.
    if body.url is not None:
        cfg = dataclasses.replace(cfg, bazarr_url=normalize_url(body.url) or None)
    if body.api_key:
        cfg = dataclasses.replace(cfg, bazarr_api_key=body.api_key)
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        raise HTTPException(status_code=400, detail="URL and API key must both be set first")
    resp = bazarr_request(cfg, "GET", "/system/status")
    if resp is None:
        raise HTTPException(status_code=502, detail="could not connect to Bazarr")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Bazarr responded {resp.status_code}")
    try:
        data = resp.json().get("data", {})
    except ValueError:
        data = {}
    return {"ok": True, "bazarr_version": data.get("bazarr_version")}
