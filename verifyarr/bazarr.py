"""Bazarr API — history lookups/blacklist for automatic cleanup, and (auto-action=remediate)
fetching a working replacement subtitle on its own. Blacklist/remediate are still
series/episodes only — but the PURE lookup functions (bazarr_current_subtitle_path_movie,
bazarr_history_score) work for both, since Bazarr's /movies endpoint has the exact same
structure as /episodes."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from verifyarr import log
from verifyarr import db
from verifyarr.settings import Config
from verifyarr.correctness import correctness_check


def bazarr_map_path(cfg: Config, local_path: Path) -> str:
    p = str(local_path)
    for local_prefix, bazarr_prefix in cfg.path_map:
        if p.startswith(local_prefix):
            return bazarr_prefix + p[len(local_prefix):]
    return p


def bazarr_to_local_path(cfg: Config, bazarr_path: str) -> Path:
    """Opposite direction of bazarr_map_path — translates a path Bazarr reports to the path
    we read the file from, via path_map. Without a path_map (the common case) the paths
    are the same."""
    for local_prefix, bazarr_prefix in cfg.path_map:
        if bazarr_path.startswith(bazarr_prefix):
            return Path(local_prefix + bazarr_path[len(bazarr_prefix):])
    return Path(bazarr_path)


def bazarr_request(cfg: Config, method: str, path: str, **kwargs):
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        return None
    headers = kwargs.pop("headers", {})
    headers["X-API-KEY"] = cfg.bazarr_api_key
    try:
        resp = requests.request(method, f"{cfg.bazarr_url}/api{path}", headers=headers, timeout=30, **kwargs)
        log.debug("Bazarr %s %s -> %d", method, path, resp.status_code)
        return resp
    except requests.RequestException as e:
        log.warning("Bazarr API call failed (%s %s): %s", method, path, e)
        return None


def bazarr_build_history_index(cfg: Config) -> dict:
    """One lookup per sweep: subtitles_path (Bazarr-side) -> newest history entry with
    provider/subs_id/ids, so existing (not just-downloaded) files can also be
    auto-blacklisted. Requires bazarr.url + bazarr.api_key."""
    index: dict[str, dict] = {}
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        return index
    for kind, endpoint, id_field in (("episode", "/episodes/history", "sonarrEpisodeId"),
                                      ("movie", "/movies/history", "radarrId")):
        resp = bazarr_request(cfg, "GET", endpoint, params={"start": 0, "length": -1})
        if resp is None or resp.status_code != 200:
            continue
        try:
            entries = resp.json().get("data", [])
        except ValueError:
            continue
        for e in entries:
            sp = e.get("subtitles_path")
            if not sp or e.get("blacklisted"):
                continue
            existing = index.get(sp)
            if existing and existing.get("timestamp", 0) >= e.get("timestamp", 0):
                continue
            index[sp] = {
                "kind": kind,
                "provider": e.get("provider"),
                "subs_id": e.get("subs_id"),
                "language": (e.get("language") or {}).get("code2") if isinstance(e.get("language"), dict) else e.get("language"),
                "series_id": e.get("sonarrSeriesId"),
                "episode_id": e.get("sonarrEpisodeId"),
                "radarr_id": e.get("radarrId"),
                "timestamp": e.get("timestamp"),
            }
    return index


class LazyHistoryIndex:
    """Defers bazarr_build_history_index's bulk /episodes/history + /movies/history fetch (Bazarr's
    ENTIRE download history log, ~10s on a real library) until the first time a lookup is actually
    needed, memoized after that for the rest of the run. handle_suspect only ever calls .get() on
    this -- most runs, especially a Scan scoped to one title/season, find nothing SUSPECT at all,
    so this often means the fetch never happens in the first place instead of always being paid
    upfront 'just in case' regardless of whether anything ends up needing it. Drop-in replacement
    for a plain dict wherever only .get() is used (pipeline.py, web/routers/files.py)."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._index: Optional[dict] = None

    def get(self, key, default=None):
        if self._index is None:
            self._index = bazarr_build_history_index(self._cfg)
            log.info("Fetched %d entries from Bazarr's history for auto-blacklist lookups", len(self._index))
        return self._index.get(key, default)


def bazarr_library_info(cfg: Config, ids_out: Optional[dict] = None) -> tuple[dict[Path, set[str]], dict[Path, str]]:
    """One bulk read of Bazarr's /movies, /series, /episodes, returning:
      - {video_path: {lang, ...}} — every embedded subtitle track Bazarr itself already knows
        about (its own "Embedded Subtitles" provider, if enabled under its Settings ->
        Providers, does this exact detection already -- it's why Bazarr sometimes skips
        downloading a language you'd expect it to fetch). Reading it here means
        discover_missing/build_library_video_rows (see discovery.py) don't need to ffprobe a
        video Bazarr already covers: one bulk read beats one subprocess call per file,
        especially on a large library over a slow/network-mounted media folder. A subtitle
        entry counts as embedded when it has an "embedded_track_id" (Bazarr's own marker for a
        track baked into the container, as opposed to a "path" for an external file).
      - {video_path: title} — Bazarr's own matched title (the real show/movie name Sonarr/
        Radarr resolved it to), used instead of guessing one from the folder/file name (see
        discovery.infer_title_and_episode, which is only ever as good as the release name it's
        parsing, e.g. "[TorrentCouch.com].The.IT.Crowd...720p.HDTV.x264"). For an episode this
        is the SHOW's title (from /series), not the individual episode's own "title" field.

    ids_out: optional dict, mutated in place with {video_path: {"kind", "series_id",
    "episode_id", "radarr_id"}} for every video Bazarr manages -- a side channel rather than a
    3rd return value, so this stays a drop-in swap everywhere the 2-tuple is already unpacked.
    Persisted into the Library cache (see discovery.build_library_video_rows,
    db.replace_library_videos) so a SUSPECT file with no Bazarr HISTORY match can still look up
    which episode to search Bazarr's providers for (see remediate_without_history below) without
    needing this whole bulk catalog fetch again.

    A video Bazarr doesn't manage (or hasn't scanned yet) simply won't be a key in either dict
    -- callers fall back to their own detection/guess for those. Both empty without
    bazarr.url + bazarr.api_key configured, same as the rest of this module's Bazarr-backed
    lookups."""
    embedded: dict[Path, set[str]] = {}
    titles: dict[Path, str] = {}
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        return embedded, titles

    def _embedded_langs(item: dict) -> set[str]:
        return {sub["code2"] for sub in (item.get("subtitles") or [])
                if sub.get("embedded_track_id") is not None and sub.get("code2")}

    def _absorb(items: list[dict], title_for, ids_for) -> None:
        for item in items:
            path = item.get("path")
            if not path:
                continue
            local = bazarr_to_local_path(cfg, path)
            langs = _embedded_langs(item)
            if langs:
                embedded.setdefault(local, set()).update(langs)
            title = title_for(item)
            if title:
                titles[local] = title
            if ids_out is not None:
                ids_out[local] = ids_for(item)

    movies_resp = bazarr_request(cfg, "GET", "/movies", params={"start": 0, "length": -1})
    if movies_resp is not None and movies_resp.status_code == 200:
        try:
            _absorb(movies_resp.json().get("data", []), title_for=lambda m: m.get("title"),
                    ids_for=lambda m: {"kind": "movie", "series_id": None, "episode_id": None,
                                        "radarr_id": m.get("radarrId")})
        except ValueError:
            pass

    series_resp = bazarr_request(cfg, "GET", "/series", params={"start": 0, "length": -1})
    series_titles: dict = {}
    series_ids: list = []
    if series_resp is not None and series_resp.status_code == 200:
        try:
            for s in series_resp.json().get("data", []):
                sid = s.get("sonarrSeriesId")
                if sid is not None:
                    series_ids.append(sid)
                    if s.get("title"):
                        series_titles[sid] = s["title"]
        except ValueError:
            pass

    # One request with every seriesid[] repeated, rather than one call per show -- /episodes
    # requires at least one seriesid[]/episodeid[] (unlike /movies, it 404s with neither).
    if series_ids:
        episodes_resp = bazarr_request(cfg, "GET", "/episodes", params={"seriesid[]": series_ids})
        if episodes_resp is not None and episodes_resp.status_code == 200:
            try:
                # An episode's own "title" field is the EPISODE's name, not the show's -- the
                # show title (what we actually want here) comes from series_titles instead.
                _absorb(episodes_resp.json().get("data", []),
                        title_for=lambda ep: series_titles.get(ep.get("sonarrSeriesId")),
                        ids_for=lambda ep: {"kind": "episode", "series_id": ep.get("sonarrSeriesId"),
                                             "episode_id": ep.get("sonarrEpisodeId"), "radarr_id": None})
            except ValueError:
                pass

    return embedded, titles


def bazarr_blacklist(cfg: Config, meta: dict) -> bool:
    if not cfg.bazarr_url or not cfg.bazarr_api_key:
        return False
    if meta.get("kind") == "movie":
        path, data = "/movies/blacklist", {
            "radarrid": meta.get("radarr_id"), "provider": meta.get("provider"),
            "subs_id": meta.get("subs_id"), "language": meta.get("language"),
            "subtitles_path": meta.get("subtitles_path"),
        }
    else:
        path, data = "/episodes/blacklist", {
            "seriesid": meta.get("series_id"), "episodeid": meta.get("episode_id"),
            "provider": meta.get("provider"), "subs_id": meta.get("subs_id"),
            "language": meta.get("language"), "subtitles_path": meta.get("subtitles_path"),
        }
    if not all([data.get("provider"), data.get("subs_id")]):
        log.warning("Cannot blacklist — missing provider/subs_id in Bazarr data for %s", meta.get("subtitles_path"))
        return False
    resp = bazarr_request(cfg, "POST", path, data=data)
    ok = resp is not None and resp.status_code in (200, 201, 204)
    if not ok:
        log.warning("Bazarr blacklist failed for %s (status %s)",
                    meta.get("subtitles_path"), getattr(resp, "status_code", "no connection"))
    return ok


def bazarr_current_subtitle_path(cfg: Config, series_id, episode_id, lang: str) -> Optional[str]:
    """Bazarr-side path to the episode's CURRENT subtitle for a language, or None if that
    language is currently missing. Used to find out what Bazarr actually fetched — the
    filename can change (e.g. .en.srt -> .en.hi.srt) depending on which release the search
    found, so we can't just reuse the original path."""
    resp = bazarr_request(cfg, "GET", "/episodes", params={"seriesid[]": series_id})
    if resp is None or resp.status_code != 200:
        return None
    try:
        episodes = resp.json().get("data", [])
    except ValueError:
        return None
    for e in episodes:
        if e.get("sonarrEpisodeId") == episode_id:
            for s in e.get("subtitles", []):
                if s.get("code2") == lang and s.get("path"):
                    return s["path"]
    return None


def bazarr_current_subtitle_path_movie(cfg: Config, radarr_id, lang: str) -> Optional[str]:
    """The movie version of bazarr_current_subtitle_path — /movies has the exact same
    subtitles[] structure as /episodes (confirmed against a real Bazarr instance), just
    without the episode level. Movie support elsewhere in bazarr.py (blacklist/remediate)
    is still series-only; only this function supports movies."""
    resp = bazarr_request(cfg, "GET", "/movies", params={"radarrid[]": radarr_id})
    if resp is None or resp.status_code != 200:
        return None
    try:
        movies = resp.json().get("data", [])
    except ValueError:
        return None
    for m in movies:
        if m.get("radarrId") == radarr_id:
            for s in m.get("subtitles", []):
                if s.get("code2") == lang and s.get("path"):
                    return s["path"]
    return None


def bazarr_history_score(cfg: Config, kind: str, subtitles_path: str) -> Optional[float]:
    """Bazarr's OWN judgment (0-100, from its matches/hash/release-group scoring) of the
    subtitle last fetched/touched at this path — extracted from the history's `score` field
    (a string like '94.17%'). None if no history entry exists for the path (e.g. a subtitle
    that was already there and never went through Bazarr)."""
    endpoint = "/movies/history" if kind == "movie" else "/episodes/history"
    resp = bazarr_request(cfg, "GET", endpoint, params={"start": 0, "length": -1})
    if resp is None or resp.status_code != 200:
        return None
    try:
        entries = resp.json().get("data", [])
    except ValueError:
        return None
    # Bazarr returns history newest-first, so the first match for this path is the latest —
    # there's no reliable sortable timestamp field in the response to double-check that with
    # (just a human string like "14 minutes ago").
    best = next((e for e in entries if e.get("subtitles_path") == subtitles_path), None)
    if best is None:
        return None
    raw = str(best.get("score") or "").rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return None


def bazarr_search_candidates(cfg: Config, episode_id, lang: str) -> list[dict]:
    """Raw candidate list from all providers, sorted by Bazarr's own score (highest first).
    Used only for MANUAL attempts — i.e. when nothing cleared minimum_score automatically,
    so we pick among candidates Bazarr would otherwise reject, using our own Whisper check
    as the judge instead of its score."""
    resp = bazarr_request(cfg, "GET", "/providers/episodes", params={"episodeid": episode_id, "language": lang})
    if resp is None or resp.status_code != 200:
        return []
    try:
        candidates = resp.json().get("data", [])
    except ValueError:
        return []
    return sorted(candidates, key=lambda c: -(c.get("score") or 0))


def bazarr_manual_download(cfg: Config, series_id, episode_id, provider: str, subtitle_id: str) -> bool:
    resp = bazarr_request(cfg, "POST", "/providers/episodes", data={
        "seriesid": series_id, "episodeid": episode_id,
        "hi": "False", "forced": "False", "original_format": "False",
        "provider": provider, "subtitle": subtitle_id,
    })
    return resp is not None and resp.status_code in (200, 204)


def bazarr_wait_for_subtitle(cfg: Config, series_id, episode_id, lang: str,
                              attempts: int = 6, delay_s: float = 2.0) -> Optional[str]:
    """Bazarr's download endpoints respond 200/204 as soon as the fetch is STARTED, not once
    the file is actually ready — checking right after can wrongly conclude 'no file came'
    even for a candidate that should have worked (seen in practice: 94-99% candidates that
    only showed up on the 3rd attempt). Polls briefly instead of giving up immediately."""
    for _ in range(attempts):
        bpath = bazarr_current_subtitle_path(cfg, series_id, episode_id, lang)
        if bpath:
            return bpath
        time.sleep(delay_s)
    return None


def verify_subtitle_candidate(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
                               conn=None, run_id: Optional[int] = None,
                               cancel_event=None) -> dict:
    """Syncs (for REAL — writes the corrected timing back to subtitle_path, via sync_pair, same
    as a normal Scan would) and correctness-checks ONE subtitle file against its video. Used by
    remediate_suspect to vet every candidate Bazarr fetches before accepting one.

    Before this, a candidate was only synced to a throwaway temp copy purely to decide
    accept/reject — an ACCEPTED replacement was left on disk exactly as Bazarr downloaded it
    (unsynced, if it needed a timing fix at all) with no `files`/correctness_history row of its
    own, invisible to the rest of the app until an unrelated later Scan happened to walk over
    it. Now the real sync always happens (a rejected candidate gets blacklisted and deleted by
    Bazarr moments later anyway, so syncing it first costs a little CPU but nothing else), and
    -- when `conn` is given -- the result is persisted (db.update_state) ONLY once a candidate
    is actually accepted, so a passing replacement is correctly synced-on-disk and shows up
    immediately, same as if a normal Scan had processed it.

    Deliberately does NOT go through correctness_and_finish/handle_suspect even on a SUSPECT
    verdict — remediate_suspect's own attempt loop is already the thing deciding whether to
    blacklist this candidate and try the next one; letting a SUSPECT verdict here independently
    trigger ANOTHER blacklist/remediate cycle would double up on that. This does mean a passing
    replacement's line-order check hasn't run yet (that's the heavier collect_samples/
    finalize_line_order path, which DOES call handle_suspect) -- it'll be picked up the next
    time a normal Scan reaches this file, same as any other file's line-order check reuses its
    cached correctness data (see pipeline.correctness_and_finish)."""
    from verifyarr.pipeline import sync_pair  # local import: pipeline.py imports FROM this module

    row, current_subs = sync_pair(video_path, subtitle_path, lang, cfg)
    if current_subs is None:
        return {"ok": False, "flag": "parse-error", "avg_score": None, "reason": row.get("note")}

    if not (cfg.enable_correctness_check and cfg.active_stt_api_key):
        return {"ok": None, "flag": "cannot verify", "avg_score": None,
                "reason": f"correctness check disabled or no {cfg.stt_provider} API key"}

    with tempfile.TemporaryDirectory() as td2:
        result = correctness_check(video_path, current_subs, lang, cfg, Path(td2), conn=conn, cancel_event=cancel_event)
    if result.get("skipped"):
        return {"ok": None, "flag": "skipped", "avg_score": None, "reason": result.get("reason")}

    row["correctness_flag"] = result["flag"]
    row["correctness_avg_score"] = round(result["avg_score"], 3) if result["avg_score"] is not None else None
    row["correctness_audio_lang"] = result.get("audio_lang")
    row["correctness_samples"] = result.get("samples")
    if result["flag"] == "ok" and conn is not None:
        db.update_state(conn, video_path, subtitle_path, row, run_id=run_id, media_root=cfg.media_root_for(subtitle_path))
    return {"ok": result["flag"] == "ok", "flag": result["flag"], "avg_score": result["avg_score"]}


def _remediate(video_path: Path, series_id, episode_id, lang: Optional[str], cfg: Config,
                tried_subs_ids: set, cancel_event=None, conn=None, run_id: Optional[int] = None,
                try_auto_download_wait: bool = True) -> str:
    """Shared implementation behind remediate_suspect and remediate_without_history below --
    the two differ only in whether there's a just-completed blacklist call whose own
    auto-download is worth waiting on first (try_auto_download_wait):
      1. (only if try_auto_download_wait) Wait up to 2 minutes for whatever Bazarr's own
         blacklist call already started downloading (deleting the old file successfully is what
         makes Bazarr auto-search for a replacement). If a file appears, test it (alass +
         Whisper, see verify_subtitle_candidate). Passes -> done.
      2. Try up to automation.remediate_max_attempts manually picked candidates from Bazarr's
         full provider search (Bazarr's own score, regardless of Bazarr's OWN minimum_score
         setting — the point is to test candidates Bazarr itself would reject, with our check
         as the judge). automation.remediate_min_score is our OWN, separate threshold on that
         same Bazarr score — candidates below it are never even downloaded.
      3. If nothing works, the language is left missing (Bazarr's normal "subtitle missing"
         state — its periodic search will try again later), and the attempt is logged in the
         returned message."""
    log_lines: list[str] = []

    def try_current_file_and_maybe_blacklist(source: str, max_wait_s: float = 12.0) -> Optional[str]:
        """Finds the episode's current subtitle for the language, tests it, and blacklists it
        if it fails. Returns a success message if it passed, otherwise None."""
        bpath = bazarr_wait_for_subtitle(cfg, series_id, episode_id, lang,
                                          attempts=max(1, int(max_wait_s // 2)), delay_s=2.0)
        if not bpath:
            log_lines.append(f"{source}: no file appeared")
            return None
        local_path = bazarr_to_local_path(cfg, bpath)
        if not local_path.exists():
            log_lines.append(f"{source}: Bazarr says {bpath}, but the file does not exist locally (path mapping?)")
            return None
        result = verify_subtitle_candidate(video_path, local_path, lang, cfg, conn=conn, run_id=run_id,
                                            cancel_event=cancel_event)
        if result["ok"]:
            log_lines.append(f"{source}: passed (score={result['avg_score']})")
            return "remediated: " + " | ".join(log_lines)
        log_lines.append(f"{source}: {result['flag']} (score={result['avg_score']})")
        # find provider/subs_id for THIS specific file via the history, so we blacklist exactly it
        hist = bazarr_request(cfg, "GET", "/episodes/history", params={"episodeid": episode_id, "length": -1})
        entry = None
        if hist is not None and hist.status_code == 200:
            try:
                for row in hist.json().get("data", []):
                    if (row.get("language") or {}).get("code2") == lang and str(row.get("subs_id")) not in map(str, tried_subs_ids):
                        entry = row
                        break
            except ValueError:
                pass
        if entry:
            tried_subs_ids.add(entry.get("subs_id"))
            bl_meta = {
                "kind": "episode", "series_id": series_id, "episode_id": episode_id,
                "provider": entry.get("provider"), "subs_id": entry.get("subs_id"),
                "language": lang, "subtitles_path": bpath,
            }
            if bazarr_blacklist(cfg, bl_meta) and conn is not None:
                db.add_blacklist_action(
                    conn, subtitle_path=str(local_path), video_path=str(video_path), kind="episode",
                    provider=entry.get("provider"), subs_id=entry.get("subs_id"), language=lang,
                    series_id=series_id, episode_id=episode_id, run_id=run_id,
                    remediation_outcome=f"rejected during remediation ({source})",
                )
        return None

    if try_auto_download_wait:
        # Give Bazarr's own automatic search a real chance before we take over manually -- its
        # search-then-pick-then-download cycle can genuinely take a while (multiple providers,
        # rate limits of its own), and jumping to manual candidates too early would blacklist
        # and skip past a perfectly good auto-fetch that was just running slow.
        result = try_current_file_and_maybe_blacklist("auto-download (from blacklist)", max_wait_s=120.0)
        if result:
            return result

    candidates = bazarr_search_candidates(cfg, episode_id, lang)
    # automation.remediate_min_score (default 80%) — Bazarr's OWN judgment of the candidate, not
    # our correctness check (that only runs after a download). Filters out candidates before ever
    # downloading them, so a clearly-bad match never even gets attempted.
    if cfg.remediate_min_score > 0:
        before = len(candidates)
        candidates = [c for c in candidates if (c.get("score") or 0) >= cfg.remediate_min_score]
        skipped = before - len(candidates)
        if skipped:
            log_lines.append(f"skipped {skipped} candidate(s) below Bazarr score {cfg.remediate_min_score}")

    attempts = 0
    for cand in candidates:
        if attempts >= cfg.remediate_max_attempts:
            break
        subs_id = cand.get("subtitle")
        if subs_id in tried_subs_ids:
            continue
        tried_subs_ids.add(subs_id)
        attempts += 1
        if not bazarr_manual_download(cfg, series_id, episode_id, cand.get("provider"), subs_id):
            log_lines.append(f"manual attempt {attempts}: download call failed ({cand.get('provider')})")
            continue
        result = try_current_file_and_maybe_blacklist(
            f"manual attempt {attempts}/{cfg.remediate_max_attempts} ({cand.get('provider')}, score={cand.get('score')})"
        )
        if result:
            return result

    total = (1 if try_auto_download_wait else 0) + attempts
    auto_part = "1 auto + " if try_auto_download_wait else ""
    return (f"no usable subtitle found after {total} attempt(s) "
            f"({auto_part}{attempts} manual) — language left as "
            f"missing. " + " | ".join(log_lines))


def remediate_suspect(subtitle_path: Path, video_path: Path, cfg: Config, media_root: Path,
                       lang: Optional[str], meta: dict, cancel_event=None,
                       conn=None, run_id: Optional[int] = None) -> str:
    """auto-action=remediate, called right after the original file has already been blacklisted
    in Bazarr (see handle_suspect in verifyarr.pipeline) -- like 'blacklist', but instead of
    just leaving the language missing, it tries to fetch a working replacement itself. See
    _remediate for the actual steps."""
    series_id, episode_id = meta.get("series_id"), meta.get("episode_id")
    if not series_id or not episode_id:
        return "cannot remediate — missing series_id/episode_id from Bazarr"
    if meta.get("kind") == "movie":
        return "cannot remediate — movies not supported yet, series only"
    return _remediate(video_path, series_id, episode_id, lang, cfg, {meta.get("subs_id")},
                       cancel_event=cancel_event, conn=conn, run_id=run_id, try_auto_download_wait=True)


def remediate_without_history(video_path: Path, cfg: Config, lang: Optional[str], series_id, episode_id,
                               cancel_event=None, conn=None, run_id: Optional[int] = None) -> str:
    """auto-action=remediate, for a SUSPECT file with no Bazarr HISTORY match to blacklist (see
    handle_suspect in verifyarr.pipeline: 'no Bazarr match' otherwise means quarantine-only,
    since there's nothing to tell Bazarr's /episodes/blacklist endpoint to remove). Most common
    causes: the current subtitle came bundled with the original release rather than through
    Bazarr at all, or its own history entry is already blacklisted from an earlier run (Bazarr
    still visibly HAS history for the episode in that case, but bazarr_build_history_index
    deliberately excludes an already-blacklisted entry, so the normal path finds nothing to
    reference).

    If Bazarr still manages this episode at all -- series_id/episode_id known from the Library
    cache (db.get_bazarr_ids_for_video, populated by an earlier whole-library scan/Detect now,
    same precondition as jobs._run_sweep's scope_roots) -- a manual provider search can still
    find and verify a replacement even though nothing could be blacklisted first. Skips straight
    to the manual-candidate step (_remediate's try_auto_download_wait=False) since there's no
    just-completed blacklist call whose own auto-download would be worth waiting on."""
    if not series_id or not episode_id:
        return "cannot search for a replacement — no Bazarr series/episode ID known for this video"
    return _remediate(video_path, series_id, episode_id, lang, cfg, set(),
                       cancel_event=cancel_event, conn=conn, run_id=run_id, try_auto_download_wait=False)
