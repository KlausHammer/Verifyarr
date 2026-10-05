"""Polls Bazarr's own "wanted" lists (scheduling.poll_new_media_enabled, on by default) to know
when a movie/episode has gone from "Bazarr still wants a subtitle for this" to "Bazarr's happy
with what it has" — whether that's a fresh download, or an existing/bundled subtitle Bazarr
judged good enough on its own. This replaced a Sonarr/Radarr-based poller (and the Sonarr/Radarr
Connect webhook) — neither is needed any more: blacklist/remediate already got everything they
need (series_id/episode_id/radarr_id) from Bazarr's own history, not a direct Sonarr/Radarr call,
and `/episodes/wanted` + `/movies/wanted` are a strictly better "is this ready yet" signal than
either the old "added" timestamp or the webhook's one-shot "does a file already exist" check.

One real gap, accepted deliberately: a bundled/embedded subtitle that already satisfies Bazarr
from the very first look never appears in "wanted" at all, so this poll never notices that
episode. That's fine — the scheduled sweep discovers every video regardless (see
discovery.discover_missing), and treats an embedded track the same way Bazarr does: as already
satisfying that language, not something to flag or fetch. Embedded tracks themselves are never
synced/verified — only external subtitle files are."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from verifyarr import db, jobs, log
from verifyarr.bazarr import bazarr_request, response_items
from verifyarr.settings import Config

_WANTED_ENDPOINTS = (("episode", "series", "/episodes/wanted"), ("movie", "movie", "/movies/wanted"))


def _wanted_keys(cfg: Config, kind: str, endpoint: str) -> set:
    """(id, lang) pairs, NOT just id — an item can be wanted for several languages at once (e.g.
    missing Danish but not English), and stays in Bazarr's wanted list until ALL of them are
    satisfied. Diffing per-language means a show stuck waiting on a language that may never turn
    up (not every release has Danish subs available) doesn't block noticing that another language
    (e.g. English) already resolved and is ready to scan."""
    resp = bazarr_request(cfg, "GET", endpoint, params={"start": 0, "length": -1})
    items = response_items(resp)
    id_field = "sonarrEpisodeId" if kind == "episode" else "radarrId"
    return {
        (i[id_field], lang.get("code2"))
        for i in items if i.get(id_field) is not None
        for lang in (i.get("missing_subtitles") or [])
        if lang.get("code2")
    }


def _resolve_pending(conn, our_kind: str, currently_wanted: set) -> None:
    """A replacement we asked for is done once Bazarr no longer lists it as wanted."""
    now = datetime.now(timezone.utc)
    wanted = {(str(i), lang) for i, lang in currently_wanted}
    done = []
    for r in db.pending_replacements(conn, PENDING_WINDOW_HOURS):
        if (r["kind"] == "movie") != (our_kind == "movie"):
            continue
        age = now - datetime.fromisoformat(r["blacklisted_at"])
        if age.total_seconds() < PENDING_GRACE_MINUTES * 60:
            continue
        key = str(r["radarr_id"] if r["kind"] == "movie" else r["episode_id"])
        if (key, r["language"]) not in wanted:
            done.append(r["id"])
    if done:
        db.resolve_replacements(conn, done)


def _poll_one(conn, cfg: Config, kind: str, our_kind: str, endpoint: str) -> None:
    key = f"scheduling._bazarr_wanted.{kind}"
    raw = db.get_setting_raw(conn, key)
    is_first_poll = raw is None
    try:
        # JSON has no tuple type — each pair round-trips as a 2-element list, converted back here.
        previously_wanted = {tuple(pair) for pair in json.loads(raw or "[]")}
    except (ValueError, TypeError):
        previously_wanted = set()

    currently_wanted = _wanted_keys(cfg, kind, endpoint)
    db.set_setting_raw(conn, key, json.dumps(sorted(currently_wanted)))

    _resolve_pending(conn, our_kind, currently_wanted)
    resolved = previously_wanted - currently_wanted
    if not resolved or is_first_poll:
        return  # first poll ever just captures a baseline — nothing "resolved" yet, just unknown

    log.info("Bazarr poll: %d %s/language pair(s) went from wanted to satisfied — scanning %s for new files",
              len(resolved), kind, our_kind)
    try:
        jobs.runner.start_sweep("bazarr_poll", force=False, kind=our_kind)
    except jobs.RunAlreadyActive:
        log.info("Bazarr poll: a job is already running, skipped this scan (%s)", our_kind)


# While we wait for a replacement we asked Bazarr for, poll fast; otherwise at the setting's pace.
PENDING_POLL_MINUTES = 3
PENDING_WINDOW_HOURS = 6  # give up waiting after this
PENDING_GRACE_MINUTES = 10  # Bazarr needs a moment to list a just-blacklisted item as wanted
_LAST_KEY = "scheduling._bazarr_poll_last"


def _due(conn, cfg: Config, now: float) -> bool:
    pending = bool(db.pending_replacements(conn, PENDING_WINDOW_HOURS))
    minutes = min(PENDING_POLL_MINUTES, cfg.poll_new_media_interval_minutes) if pending \
        else cfg.poll_new_media_interval_minutes
    try:
        last = float(db.get_setting_raw(conn, _LAST_KEY) or 0)
    except ValueError:
        last = 0.0
    return now - last >= minutes * 60 - 5


def poll_wanted_subtitles() -> None:
    """Ticks every PENDING_POLL_MINUTES (scheduler.py) but only asks Bazarr when due (see _due).
    No-ops quietly if the setting is off or Bazarr isn't configured."""
    conn = db.connect()
    try:
        cfg = Config.from_db(conn)
        if not cfg.poll_new_media_enabled or not cfg.bazarr_url or not cfg.bazarr_api_key:
            return
        now = time.time()
        if not _due(conn, cfg, now):
            return
        db.set_setting_raw(conn, _LAST_KEY, str(now))
        for kind, our_kind, endpoint in _WANTED_ENDPOINTS:
            _poll_one(conn, cfg, kind, our_kind, endpoint)
    except Exception as e:
        log.warning("Bazarr wanted-subtitles poll failed: %s", e)
    finally:
        conn.close()
