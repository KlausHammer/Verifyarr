"""CLI — `sweep` (periodic scan of the whole library), `single` (one file pair, called from
Bazarr's post-processing hook), and `reset-password` (emergency exit for a forgotten admin
password, see verifyarr.auth). `verifyarr.py` in the repo root is a thin shim into this.

The CLI and the webapp share the same settings table (verifyarr.db, see Config.from_db) and
the same runs/files tables, so a run triggered here shows up in the webapp's Activity list
just like one triggered from the UI or the scheduler."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr import db
from verifyarr import jobs
from verifyarr import auth
from verifyarr.settings import (Config, DEFAULT_DB_PATH, clean_lang_code,
                                import_from_env_once, migrate_log_level_once)


def cmd_sweep(cfg: Config, conn, force: bool, trigger: str = "cli_sweep") -> None:
    run_id = jobs.create_run(conn, trigger, "sweep", cfg.dry_run, force)
    jobs.execute_run(run_id, cfg, conn, threading.Event(), "sweep", trigger=trigger, force=force)


def cmd_single(cfg: Config, conn, video: str, subtitle: str, lang: Optional[str],
               provider: Optional[str], subs_id: Optional[str],
               series_id: Optional[str], episode_id: Optional[str],
               radarr_id: Optional[str], trigger: str = "cli_single") -> None:
    video_p, subtitle_p = Path(video), Path(subtitle)
    if not video_p.exists() or not subtitle_p.exists():
        log.error("Video or subtitle does not exist: %s / %s", video_p, subtitle_p)
        sys.exit(1)

    bazarr_meta = None
    if provider and subs_id:
        bazarr_meta = {
            "kind": "movie" if radarr_id else "episode",
            "provider": provider, "subs_id": subs_id, "language": lang,
            "series_id": series_id, "episode_id": episode_id, "radarr_id": radarr_id,
        }

    from verifyarr.discovery import target_label
    run_id = jobs.create_run(conn, trigger, "single", cfg.dry_run, False,
                              target_kind=cfg.kind_for(video_p),
                              target_title=target_label(video_p, cfg.media_root_for(video_p)))
    jobs.execute_run(run_id, cfg, conn, threading.Event(), "single", trigger=trigger,
                      video=video_p, subtitle=subtitle_p, lang=lang, bazarr_meta=bazarr_meta)


def cmd_generate(cfg: Config, conn, video: str, lang: str, trigger: str = "cli_generate") -> None:
    """Manual/scriptable trigger for generate.generate_one — useful for testing a provider/
    chunk-size setup end-to-end without going through the webapp UI. Chains straight into the
    ordinary sync pipeline afterward, same as the Files page's "Generate" button (see
    jobs._run_generate_single).

    Like that button, this deliberately ignores the per-(video, lang) retry cooldown and the
    daily cap that the automatic sweep respects -- someone asking for this one file right now is
    its own answer, the same split between manual and automatic triggers this app applies
    everywhere else (see jobs._AUTO_TRIGGERS)."""
    video_p = Path(video)
    if not video_p.exists():
        log.error("Video does not exist: %s", video_p)
        sys.exit(1)
    if not cfg.generate_enabled:
        log.error("generate.enabled is off — turn it on under Settings -> Generate first.")
        sys.exit(1)
    if not clean_lang_code(lang):
        log.error("Not a usable subtitle language code: %r — use a short code like 'en' or 'da'.", lang)
        sys.exit(1)
    lang = lang.lower()

    from verifyarr.discovery import target_label
    run_id = jobs.create_run(conn, trigger, "generate_single", cfg.dry_run, False,
                              target_kind=cfg.kind_for(video_p),
                              target_title=target_label(video_p, cfg.media_root_for(video_p)))
    jobs.execute_run(run_id, cfg, conn, threading.Event(), "generate_single", trigger=trigger,
                      video=video_p, lang=lang)


def cmd_reset_password() -> None:
    conn = db.connect()
    try:
        auth.reset_all_users(conn)
    finally:
        conn.close()
    log.info("All admin users deleted — the web interface will show the setup screen on next visit.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Subtitle sync and correctness check")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_sweep = sub.add_parser("sweep", help="Scan the whole library (Settings -> General -> Root Folders)")
    p_sweep.add_argument("--force", action="store_true", help="Ignore saved state, process everything again")

    p_single = sub.add_parser("single", help="Process one video/subtitle pair (for Bazarr post-processing)")
    p_single.add_argument("--video", required=True)
    p_single.add_argument("--subtitle", required=True)
    p_single.add_argument("--lang", default=None)
    # These map to Bazarr's post-processing placeholders — check the exact names under
    # Settings -> General -> Post-processing in your own Bazarr; they can vary slightly
    # between episodes/movies and between versions.
    p_single.add_argument("--provider", default=None)
    p_single.add_argument("--subs-id", default=None)
    p_single.add_argument("--series-id", default=None)
    p_single.add_argument("--episode-id", default=None)
    p_single.add_argument("--radarr-id", default=None)

    p_generate = sub.add_parser("generate", help="Generate a missing subtitle via Whisper (+ LLM translation) for one video")
    p_generate.add_argument("--video", required=True)
    p_generate.add_argument("--lang", required=True)

    sub.add_parser("reset-password", help="Emergency exit: delete admin login so the web interface shows the setup screen again")

    args = parser.parse_args()

    if args.mode == "reset-password":
        cmd_reset_password()
        return

    conn = db.connect(DEFAULT_DB_PATH)
    migrate_log_level_once(conn)
    import_from_env_once(conn)
    cfg = Config.from_db(conn)
    logging.getLogger("verifyarr").setLevel(cfg.log_level)
    try:
        if args.mode == "sweep":
            cmd_sweep(cfg, conn, force=args.force)
        elif args.mode == "single":
            cmd_single(cfg, conn, args.video, args.subtitle, args.lang,
                       args.provider, args.subs_id, args.series_id, args.episode_id, args.radarr_id)
        elif args.mode == "generate":
            cmd_generate(cfg, conn, args.video, args.lang)
    finally:
        conn.close()
