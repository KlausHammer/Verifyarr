"""Polls Bazarr's own "wanted" lists (scheduling.poll_new_media_enabled, on by default) to know
when a movie/episode has gone from "Bazarr still wants a subtitle for this" to "Bazarr's happy
with what it has" — whether that's a fresh download, or an existing/bundled subtitle Bazarr
judged good enough on its own. This replaced a Sonarr/Radarr-based poller (and the Sonarr/Radarr
Connect webhook) — neither is needed any more: blacklist/remediate already got everything they
need (series_id/episode_id/radarr_id) from Bazarr's own history, not a direct Sonarr/Radarr call,
and `/episodes/wanted` + `/movies/wanted` are a strictly better "is this ready yet" signal than
either the old "added" timestamp or the webhook's one-shot "does a file already exist" check.

One real gap, accepted deliberately: a bundled/embedded subtitle that already satisfies Bazarr
from the very first look never appears in "wanted" at all, so it's never noticed here — that
still gets picked up eventually by the regular scheduled sweep. Embedded subtitle tracks
themselves are never checked at all (assumed to already fit the media, by design)."""

from __future__ import annotations

import json

from verifyarr import db, jobs, log
from verifyarr.bazarr import bazarr_request
from verifyarr.settings import Config

_WANTED_ENDPOINTS = (("episode", "series", "/episodes/wanted"), ("movie", "movie", "/movies/wanted"))


def _wanted_keys(cfg: Config, kind: str, endpoint: str) -> set:
    """(id, lang) pairs, NOT just id — an item can be wanted for several languages at once (e.g.
    missing Danish but not English), and stays in Bazarr's wanted list until ALL of them are
    satisfied. Diffing per-language means a show stuck waiting on a language that may never turn
    up (not every release has Danish subs available) doesn't block noticing that another language
    (e.g. English) already resolved and is ready to scan."""
    resp = bazarr_request(cfg, "GET", endpoint, params={"start": 0, "length": -1})
    if resp is None or resp.status_code != 200:
        return set()
    try:
        items = resp.json().get("data", [])
    except ValueError:
        return set()
    id_field = "sonarrEpisodeId" if kind == "episode" else "radarrId"
    return {
        (i[id_field], lang.get("code2"))
        for i in items if i.get(id_field) is not None
        for lang in (i.get("missing_subtitles") or [])
        if lang.get("code2")
    }


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

    resolved = previously_wanted - currently_wanted
    if not resolved or is_first_poll:
        return  # first poll ever just captures a baseline — nothing "resolved" yet, just unknown

    log.info("Bazarr poll: %d %s/language pair(s) went from wanted to satisfied — scanning %s for new files",
              len(resolved), kind, our_kind)
    try:
        jobs.runner.start_sweep("bazarr_poll", force=False, kind=our_kind)
    except jobs.RunAlreadyActive:
        log.info("Bazarr poll: a job is already running, skipped this scan (%s)", our_kind)


def poll_wanted_subtitles() -> None:
    """Called on a fixed interval by scheduler.py. No-ops quietly if the setting is off or Bazarr
    isn't configured — this runs regardless of whether anyone uses the feature."""
    conn = db.connect()
    try:
        cfg = Config.from_db(conn)
        if not cfg.poll_new_media_enabled or not cfg.bazarr_url or not cfg.bazarr_api_key:
            return
        for kind, our_kind, endpoint in _WANTED_ENDPOINTS:
            _poll_one(conn, cfg, kind, our_kind, endpoint)
    except Exception as e:
        log.warning("Bazarr wanted-subtitles poll failed: %s", e)
    finally:
        conn.close()
