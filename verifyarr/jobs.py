"""Job execution — sweep/single. The shared core (`execute_run`) is used SYNCHRONOUSLY by
the CLI (same thread, with a cancel_event that's never set) and ASYNCHRONOUSLY by the webapp
via `JobRunner` (a background thread + real cancellation) — both paths end up in the same
runs/run_log_lines/files tables, regardless of who triggered the run.

Cancellation is cooperative: checked between files in the sweep loop, and inside Groq's
rate-limit wait logic (see correctness._post_ratelimited) — NOT mid alass-subprocess."""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr import db
from verifyarr.settings import Config
from verifyarr.discovery import (discover_pairs, discover_all_videos, discover_missing,
                                parse_lang_from_filename, build_library_video_rows,
                                infer_title_and_episode)
from verifyarr.pipeline import process_pair
from verifyarr.reports import write_report
from verifyarr.bazarr import bazarr_build_history_index
from verifyarr.correctness import JobCancelled

RunAlreadyActive = db.RunAlreadyActive


class _RunLogHandler(logging.Handler):
    """Captures the ordinary log.info/warning/exception calls from the whole pipeline and
    stores them in run_log_lines while this specific job runs — this is what Activity's
    live/historical log view (and the SSE stream) actually reads from."""

    def __init__(self, conn: sqlite3.Connection, run_id: int):
        super().__init__()
        self.conn, self.run_id = conn, run_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            db.add_log_line(self.conn, self.run_id, record.levelname, self.format(record))
        except Exception:
            pass


def _apply_dry_run(cfg: Config, dry_run: bool) -> Config:
    return cfg if cfg.dry_run == dry_run else dataclasses.replace(cfg, dry_run=dry_run)


# Triggers that run WITHOUT a human choosing, right then, what to scan — the scheduled sweep
# (trigger="scheduled") AND the Bazarr wanted-subtitles poll (trigger="bazarr_poll") share ONE set
# of switches (general.auto_scan_*) for exactly this reason: from the user's point of view they're
# both "the system decided to scan something on its own", just on different cadences. Manual
# Scan/Rescan (trigger="manual_ui") and CLI calls are NOT in this set — a human just asked for
# this scan right now, so it gets its OWN, separate switches (sync_enabled/
# enable_correctness_check/line_order_enabled), untouched by whatever the automatic switches say.
# See Settings -> Automation's "What runs" table.
_AUTO_TRIGGERS = {"scheduled", "bazarr_poll"}


def _effective_cfg(cfg: Config, trigger: str) -> Config:
    """See general.auto_scan_* (Settings -> Automation). Only the scheduled sweep and the Bazarr
    poll get this — a manual Scan always uses sync_enabled/enable_correctness_check/
    line_order_enabled as-is, its own independent switch (see _AUTO_TRIGGERS above)."""
    if trigger not in _AUTO_TRIGGERS:
        return cfg
    return dataclasses.replace(
        cfg,
        sync_enabled=cfg.auto_scan_sync_enabled,
        enable_correctness_check=cfg.auto_scan_correctness_enabled,
        line_order_enabled=cfg.auto_scan_line_order_enabled,
    )


def create_run(conn: sqlite3.Connection, trigger: str, mode: str, dry_run: bool, force: bool,
                target_kind: Optional[str] = None, target_title: Optional[str] = None) -> int:
    """Thin wrapper around db.create_run — its own function here so both the CLI and
    JobRunner can import it from one place (jobs.py) without knowing about db.RunAlreadyActive."""
    return db.create_run(conn, trigger, mode, dry_run, force, target_kind, target_title)


def execute_run(run_id: int, cfg: Config, conn: sqlite3.Connection, cancel_event: threading.Event,
                 mode: str, *, trigger: str = "", force: bool = False, video: Optional[Path] = None,
                 subtitle: Optional[Path] = None, lang: Optional[str] = None,
                 bazarr_meta: Optional[dict] = None,
                 kind: Optional[str] = None, title: Optional[str] = None,
                 season: Optional[str] = None) -> None:
    cfg = _effective_cfg(cfg, trigger)
    handler = _RunLogHandler(conn, run_id)
    root_log = logging.getLogger("verifyarr")
    root_log.addHandler(handler)
    status, error_message = "completed", None
    try:
        if mode == "sweep":
            _run_sweep(conn, run_id, cfg, force, cancel_event, kind=kind, title=title, season=season)
        else:
            _run_single(conn, run_id, cfg, video, subtitle, lang, bazarr_meta, cancel_event)
    except JobCancelled:
        status = "cancelled"
    except Exception as e:
        log.exception("Job %s failed: %s", run_id, e)
        status, error_message = "failed", str(e)
    else:
        if cancel_event.is_set():
            status = "cancelled"
    finally:
        root_log.removeHandler(handler)
        db.finish_run(conn, run_id, status, error_message)


def _run_sweep(conn: sqlite3.Connection, run_id: int, cfg: Config, force: bool,
                cancel_event: threading.Event, kind: Optional[str] = None,
                title: Optional[str] = None, season: Optional[str] = None) -> None:
    pairs = discover_pairs(cfg)
    all_videos = discover_all_videos(cfg)
    missing = discover_missing(cfg, pairs, all_videos)
    log.info("Found %d video/subtitle pairs and %d missing languages under %s",
              len(pairs), len(missing), cfg.media_roots)

    # Library "Scan"/"Rescan" (see web/routers/library.py) can restrict a sweep to one kind
    # (Movies/Series), one title, and/or (series only) one season — everything above (discovery,
    # missing-marking, the library cache refresh below) stays whole-tree regardless, only the
    # actual processing loop is narrowed, so scoping a run never leaves the rest of the library's
    # cached state stale.
    scoped_pairs = pairs
    if kind:
        scoped_pairs = [p for p in scoped_pairs if cfg.kind_for(p[0]) == kind]
    if title:
        def _pair_title(p: tuple) -> str:
            _se, t = infer_title_and_episode(p[0])
            return t or str(p[0].parent)
        scoped_pairs = [p for p in scoped_pairs if _pair_title(p) == title]
    if season:
        def _pair_season(p: tuple) -> str:
            se, _t = infer_title_and_episode(p[0])
            return (se or "")[:3]
        scoped_pairs = [p for p in scoped_pairs if _pair_season(p) == season]
    if kind or title or season:
        log.info("Scoped to %d/%d pairs (kind=%s, title=%s, season=%s)",
                  len(scoped_pairs), len(pairs), kind, title, season)

    db.update_run_counts(conn, run_id, files_total=len(scoped_pairs))

    # We already walked the tree above — reuse it to refresh the Library page's cache (it
    # does NOT scan itself per page load, see web/routers/library.py), instead of requiring
    # a separate rescan click to discover files added since the last sweep.
    db.replace_library_videos(conn, build_library_video_rows(cfg, pairs, all_videos))

    for video, lang in missing:
        db.mark_missing(conn, video, lang, cfg.media_root_for(video))

    # BUG FIX (found during optimization review): "remediate" needs the history index just as much
    # as "blacklist" does — handle_suspect only falls back to it when no bazarr_meta was passed in,
    # which is always the case for sweep-triggered files (bazarr_meta only exists for the Bazarr
    # post-processing hook's single-file path). Before this fix, a sweep with
    # auto_action=remediate silently never remediated anything: history_index stayed None for the
    # whole run, so every SUSPECT file hit "no Bazarr match" regardless of whether Bazarr actually
    # had history for it (manual per-file Remediate from Files/FileDetail was unaffected — it
    # builds its own history_index directly, see routers/files.py). Checked against EITHER
    # per-check action (correctness_auto_action / line_order_auto_action, see pipeline.py) — a
    # sweep can produce either kind of SUSPECT, so the history index has to be ready for both.
    history_index = bazarr_build_history_index(cfg) if cfg.correctness_auto_action in ("blacklist", "remediate") \
        or cfg.line_order_auto_action in ("blacklist", "remediate") else None
    if history_index is not None:
        log.info("Fetched %d entries from Bazarr's history for auto-blacklist lookups", len(history_index))

    rows = []
    # audio_cache: see verifyarr.cli — shares one audio decode per video across languages (for alass).
    audio_cache: dict = {}
    # Whisper transcription for the correctness check is NO LONGER shared across a video's
    # subtitle languages (it used to be, via a now-removed transcript_cache argument) — since
    # the correctness check always collects line-order candidates at the same time (see
    # line_order.py), clip placement is decided by EACH language's own two-line candidates,
    # so the actually chosen timestamps rarely match between languages anyway. In exchange,
    # the result is now cached PER SUBTITLE across runs (line_order_cache_key/json in the
    # files table) — an unchanged subtitle costs zero Whisper calls on a later sweep,
    # regardless of language.
    with tempfile.TemporaryDirectory(prefix="verifyarr-audio-") as audio_cache_dir_str:
        audio_cache_dir = Path(audio_cache_dir_str)
        for i, (video, subtitle, lang) in enumerate(scoped_pairs, 1):
            if cancel_event.is_set():
                log.warning("Job cancelled — stopping before file %d/%d", i, len(scoped_pairs))
                raise JobCancelled("cancelled between files")
            if not force and db.should_skip(conn, video, subtitle):
                continue
            log.info("[%d/%d] %s", i, len(scoped_pairs), subtitle)
            try:
                row = process_pair(video, subtitle, lang, cfg, conn, history_index=history_index,
                                    audio_cache=audio_cache, audio_cache_dir=audio_cache_dir,
                                    run_id=run_id, cancel_event=cancel_event)
            except JobCancelled:
                raise
            except Exception as e:  # one file's error must not stop the whole sweep
                log.exception("Unexpected error for %s: %s", subtitle, e)
                row = {
                    "video": str(video), "subtitle": str(subtitle), "lang": lang or "",
                    "sync_status": "unexpected-error", "sync_max_shift_s": None, "structural_change": False,
                    "sync_split_blocks": None, "correctness_flag": "-", "correctness_avg_score": None,
                    "note": str(e), "auto_action": "-",
                }
                db.update_state(conn, video, subtitle, row, run_id=run_id, media_root=cfg.media_root_for(subtitle))
            rows.append(row)
            db.bump_run_progress(conn, run_id, row)
    if rows:
        write_report(rows, cfg.report_dir)
    else:
        log.info("Nothing to do — everything has already been processed (use force to run it all again).")


def _run_single(conn: sqlite3.Connection, run_id: int, cfg: Config, video: Path, subtitle: Path,
                 lang: Optional[str], bazarr_meta: Optional[dict], cancel_event: threading.Event) -> None:
    if not lang:
        lang = parse_lang_from_filename(subtitle)
    row = process_pair(video, subtitle, lang, cfg, conn, bazarr_meta=bazarr_meta,
                        run_id=run_id, cancel_event=cancel_event)
    write_report([row], cfg.report_dir)
    db.bump_run_progress(conn, run_id, row)


class JobRunner:
    """Tracks the ONE job allowed to run at a time in the webapp. `runner` below is this
    module's singleton, used by routers/runs.py and scheduler.py."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None
        self._current_run_id: Optional[int] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current_run_id(self) -> Optional[int]:
        return self._current_run_id

    def cancel(self) -> bool:
        with self._lock:
            if self._cancel_event is None:
                return False
            self._cancel_event.set()
            return True

    def start_sweep(self, trigger: str, force: bool = False, dry_run_override: Optional[bool] = None,
                     kind: Optional[str] = None, title: Optional[str] = None,
                     season: Optional[str] = None) -> int:
        return self._start(trigger, "sweep", force=force, dry_run_override=dry_run_override,
                            kind=kind, title=title, season=season)

    def start_single(self, trigger: str, video: Path, subtitle: Path, lang: Optional[str] = None,
                      bazarr_meta: Optional[dict] = None, dry_run_override: Optional[bool] = None) -> int:
        return self._start(trigger, "single", video=video, subtitle=subtitle, lang=lang,
                            bazarr_meta=bazarr_meta, dry_run_override=dry_run_override)

    def _start(self, trigger: str, mode: str, *, dry_run_override: Optional[bool] = None, **kwargs) -> int:
        with self._lock:
            if self.is_running():
                raise RunAlreadyActive("a job is already running")
            conn0 = db.connect()
            try:
                cfg = Config.from_db(conn0)
                dry_run = cfg.dry_run if dry_run_override is None else dry_run_override
                cfg = _apply_dry_run(cfg, dry_run)
                # target_kind/target_title — what shows in Activity's Type/Target columns. For a
                # sweep, that's whatever scope was requested (see routers/library.py); for a
                # single-file run it's derived from the video path itself.
                if mode == "sweep":
                    target_kind, target_title = kwargs.get("kind"), kwargs.get("title")
                    if target_title and kwargs.get("season"):
                        target_title = f"{target_title} {kwargs['season']}"
                elif mode == "single" and kwargs.get("video") is not None:
                    from verifyarr.discovery import target_label
                    target_kind = cfg.kind_for(kwargs["video"])
                    target_title = target_label(kwargs["video"])
                else:
                    target_kind = target_title = None
                run_id = create_run(conn0, trigger, mode, dry_run, kwargs.get("force", False),
                                     target_kind=target_kind, target_title=target_title)
            finally:
                conn0.close()

            cancel_event = threading.Event()
            self._cancel_event = cancel_event
            self._current_run_id = run_id

            def target():
                conn = db.connect()
                try:
                    execute_run(run_id, cfg, conn, cancel_event, mode, trigger=trigger, **kwargs)
                finally:
                    conn.close()
                    with self._lock:
                        self._thread = None
                        self._cancel_event = None
                        self._current_run_id = None

            self._thread = threading.Thread(target=target, name=f"verifyarr-job-{run_id}", daemon=True)
            self._thread.start()
            return run_id


runner = JobRunner()
