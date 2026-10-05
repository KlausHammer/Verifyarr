"""Scheduling — APScheduler in-process, reads cron syntax straight from settings.scheduling.cron.
No restart needed on a schedule change: `reschedule()` is called from routers/settings.py
whenever the scheduling group is saved."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from verifyarr import bazarr_poll
from verifyarr import library_poll
from verifyarr import log
from verifyarr import db
from verifyarr import jobs
from verifyarr.settings import Config

_JOB_ID = "scheduled-sweep"
_POLL_JOB_ID = "poll-new-media"
_LIBRARY_POLL_JOB_ID = "poll-library"
_TRANSCRIPT_PRUNE_JOB_ID = "prune-transcript-cache"
_FULL_TRANSCRIPT_PRUNE_JOB_ID = "prune-full-transcript-cache"
_scheduler: Optional[BackgroundScheduler] = None


def _run_scheduled_sweep() -> None:
    if jobs.runner.is_running():
        log.info("Scheduled sweep skipped — another job is already running")
        return
    try:
        jobs.runner.start_sweep(trigger="scheduled", force=False)
    except jobs.RunAlreadyActive:
        log.info("Scheduled sweep skipped — another job started just before")


def _prune_with_fresh_conn(prune_fn, max_age_days: int, item_desc: str) -> None:
    """One prune pass on its own connection: open, prune, log only when something was
    actually removed, always close. All three daily cache prunes share this so the
    connect/try/log/finally shape exists once."""
    conn = db.connect()
    try:
        removed = prune_fn(conn, max_age_days=max_age_days)
        if removed:
            log.info("Pruned %d %s older than %d days", removed, item_desc, max_age_days)
    finally:
        conn.close()


def _prune_transcript_cache_job() -> None:
    """video_transcript_cache (see correctness.correctness_check) has no settings knob -- fixed
    at 30 days, same as the reasoning behind reports.MAX_REPORTS not being one either."""
    _prune_with_fresh_conn(db.prune_transcript_cache, 30, "cached transcript(s)")
    _prune_with_fresh_conn(db.prune_gap_probe_cache, 30, "cached gap probe(s)")


def _prune_full_transcript_cache_job() -> None:
    """video_full_transcript_cache (see generate.py) -- a much longer retention than the ordinary
    sample-clip cache (90 vs 30 days): a full-track transcript is far more expensive to
    regenerate (a whole video's worth of Whisper calls, not one 30s clip)."""
    _prune_with_fresh_conn(db.prune_full_transcript_cache, 90, "cached full transcript(s)")
    _prune_with_fresh_conn(db.prune_vad_timeline_cache, 90, "cached VAD timeline(s)")
    # Same daily pass cleans up generate_attempts -- a record older than 30 days is long past
    # both the retry cooldown and the 24-hour cap window it exists for (see generate.py).
    _prune_with_fresh_conn(db.prune_generate_attempts, 30,
                            "subtitle-generation attempt record(s)")


_CRON_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _cron_dow(field: str) -> str:
    """Standard cron day-of-week (0/7 = Sunday) as APScheduler names; APScheduler's own
    numbers start at Monday, so '0' would fire on Mondays."""
    if field in ("0-6", "0-7", "1-7"):
        return "*"
    return re.sub(r"\d+", lambda m: _CRON_DAYS[int(m.group())] if int(m.group()) <= 7
                  else m.group(), field)


def _cron_to_trigger(cron_expr: str) -> CronTrigger:
    # Standard 5-field cron in the container's local time (TZ), which is what users set.
    minute, hour, day, month, dow = cron_expr.split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=_cron_dow(dow))


def next_sweep_at(cron_expr: str, now=None) -> Optional[str]:
    """Next sweep fire time as ISO ("Next scan"), from the cron alone. Carries the server's
    UTC offset so the browser renders it in the viewer's own zone. None if invalid."""
    from datetime import timezone
    if not (cron_expr or "").strip():
        return None
    try:
        trigger = _cron_to_trigger(cron_expr)
    except Exception:
        return None
    fire = trigger.get_next_fire_time(None, now or datetime.now(timezone.utc))
    return fire.isoformat() if fire else None


def server_timezone_name() -> str:
    """Server timezone name (e.g. "Europe/Copenhagen"). Prefers $TZ, else the local zone."""
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    try:
        from tzlocal import get_localzone
        return str(get_localzone())
    except Exception:
        return datetime.now().astimezone().tzname() or "UTC"


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    # Local zone (the container's TZ), not UTC: the cron triggers resolve against it.
    _scheduler = BackgroundScheduler()
    _scheduler.start()
    reschedule()
    return _scheduler


def reschedule() -> None:
    """Reads the current scheduling settings and updates all running scheduler jobs (the
    cron-based sweep plus both poll intervals). Called at startup and again whenever
    settings/scheduling is saved, so a changed schedule takes effect without a restart."""
    if _scheduler is None:
        return
    conn = db.connect()
    try:
        cfg = Config.from_db(conn)
    finally:
        conn.close()

    if not cfg.sweep_cron.strip():  # empty = no scheduled sweep
        if _scheduler.get_job(_JOB_ID):
            _scheduler.remove_job(_JOB_ID)
        log.info("Scheduled sweep: off")
    else:
        try:
            trigger = _cron_to_trigger(cfg.sweep_cron)
        except Exception as e:
            log.warning("Invalid cron expression in schedule.cron (%r): %s — schedule not changed", cfg.sweep_cron, e)
        else:
            _scheduler.add_job(_run_scheduled_sweep, trigger, id=_JOB_ID, replace_existing=True, max_instances=1)
            log.info("Scheduled sweep set to: %s (server time, %s)", cfg.sweep_cron,
                     server_timezone_name())

    # Enable/disable itself is also checked live inside bazarr_poll.poll_wanted_subtitles()
    # (belt and braces), but the INTERVAL can only change here — APScheduler needs a fresh
    # trigger for that.
    interval = bazarr_poll.PENDING_POLL_MINUTES  # tick; the poll itself decides when it is due
    _scheduler.add_job(bazarr_poll.poll_wanted_subtitles, IntervalTrigger(minutes=interval),
                        id=_POLL_JOB_ID, replace_existing=True, max_instances=1)
    log.info("Bazarr wanted-subtitles poll: %s, every %d min",
              "on" if cfg.poll_new_media_enabled else "off", cfg.poll_new_media_interval_minutes)

    # Same belt-and-braces note as above — enable/disable is also checked live inside
    # library_poll.poll_library_for_new_media(), but the interval needs a fresh trigger here.
    library_interval = max(1, cfg.poll_library_interval_minutes)
    _scheduler.add_job(library_poll.poll_library_for_new_media, IntervalTrigger(minutes=library_interval),
                        id=_LIBRARY_POLL_JOB_ID, replace_existing=True, max_instances=1)
    log.info("Library poll (new files on disk): %s, every %d min",
              "on" if cfg.poll_library_enabled else "off", library_interval)

    # Always on, no settings knob (see _prune_transcript_cache_job) -- re-added here too since
    # reschedule() re-adds every job on each call, harmless with replace_existing=True.
    _scheduler.add_job(_prune_transcript_cache_job, IntervalTrigger(hours=24),
                        id=_TRANSCRIPT_PRUNE_JOB_ID, replace_existing=True, max_instances=1)
    _scheduler.add_job(_prune_full_transcript_cache_job, IntervalTrigger(hours=24),
                        id=_FULL_TRANSCRIPT_PRUNE_JOB_ID, replace_existing=True, max_instances=1)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
