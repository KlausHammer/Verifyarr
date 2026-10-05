"""Settings — all app configuration lives here instead of docker-compose.yml, so compose
only holds real Docker requirements."""

from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from verifyarr import gpu
from verifyarr import settings as settings_mod
from verifyarr import scheduler
from verifyarr.bazarr import bazarr_request, suggest_mapping, bazarr_to_local_path, response_items
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


@router.get("/bazarr/path-check")
def bazarr_path_check(user=Depends(require_auth), conn=Depends(get_conn)):
    """One movie and one episode as Bazarr reports them, next to the library's copy of the same
    file, and the path mapping that would make them match."""
    cfg = Config.from_db(conn)
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        return {"configured": False, "samples": []}
    samples = []
    movies = response_items(bazarr_request(cfg, "GET", "/movies", params={"start": 0, "length": 1}))
    series = response_items(bazarr_request(cfg, "GET", "/series", params={"start": 0, "length": 1}))
    episodes = []
    if series and series[0].get("sonarrSeriesId") is not None:
        episodes = response_items(bazarr_request(cfg, "GET", "/episodes",
                                                 params={"seriesid[]": [series[0]["sonarrSeriesId"]]}))
    for kind, items in (("movie", movies), ("series", episodes)):
        path = (items[0].get("path") if items else None) or None
        if not path:
            continue
        mapped = str(bazarr_to_local_path(cfg, path))
        sample = {"kind": kind, "bazarr_path": path, "as_local": mapped, "exists": Path(mapped).exists(),
                  "library_path": None, "suggestion": None}
        row = None
        for r in conn.execute("SELECT video_path FROM library_videos WHERE kind = ?", (kind,)):
            if PurePosixPath(r["video_path"]).name == PurePosixPath(path).name:
                row = r
                break
        if row is not None:
            sample["library_path"] = row["video_path"]
            if not sample["exists"]:
                sample["suggestion"] = suggest_mapping(row["video_path"], path)
        samples.append(sample)
    return {"configured": True, "samples": samples}


@router.get("/whisper-status")
def whisper_status(user=Depends(require_auth)):
    """Whether local Whisper found a GPU (probed at startup, see gpu.py)."""
    return gpu.status()


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
    try:
        settings_mod.set_settings_group(conn, group, body.values)
    except ValueError as e:
        # set_settings_group raises ValueError for a value the user can fix (a bad model
        # path, an unknown log level, ...) -- a 422 with the reason, not a 500.
        raise HTTPException(status_code=422, detail=str(e))
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
        hint = " -- check the port and Bazarr's Base URL (e.g. http://host:6767/bazarr)" if resp.status_code == 404 else ""
        raise HTTPException(status_code=502,
                            detail=f"Bazarr responded {resp.status_code} for {cfg.bazarr_url}/api/system/status{hint}")
    try:
        data = resp.json().get("data", {})
    except ValueError:
        data = {}
    return {"ok": True, "bazarr_version": data.get("bazarr_version")}
