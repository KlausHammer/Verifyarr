"""Scheduling — APScheduler in-process, reads cron syntax straight from settings.scheduling.cron.
No restart needed on a schedule change: `reschedule()` is called from routers/settings.py
whenever the scheduling group is saved."""

from __future__ import annotations

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
_scheduler: Optional[BackgroundScheduler] = None


def _run_scheduled_sweep() -> None:
    if jobs.runner.is_running():
        log.info("Scheduled sweep skipped — another job is already running")
        return
    try:
        jobs.runner.start_sweep(trigger="scheduled", force=False)
    except jobs.RunAlreadyActive:
        log.info("Scheduled sweep skipped — another job started just before")


def _prune_transcript_cache_job() -> None:
    """video_transcript_cache (see correctness.correctness_check) has no settings knob -- fixed
    at 30 days, same as the reasoning behind reports.MAX_REPORTS not being one either."""
    conn = db.connect()
    try:
        removed = db.prune_transcript_cache(conn, max_age_days=30)
        if removed:
            log.info("Pruned %d cached transcript(s) older than 30 days", removed)
    finally:
        conn.close()


def _cron_to_trigger(cron_expr: str) -> CronTrigger:
    # Standard 5-field cron (minute hour day-of-month month day-of-week) — APScheduler's
    # CronTrigger.from_crontab covers exactly this format.
    return CronTrigger.from_crontab(cron_expr)


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
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

    try:
        trigger = _cron_to_trigger(cfg.sweep_cron)
    except Exception as e:
        log.warning("Invalid cron expression in schedule.cron (%r): %s — schedule not changed", cfg.sweep_cron, e)
    else:
        _scheduler.add_job(_run_scheduled_sweep, trigger, id=_JOB_ID, replace_existing=True, max_instances=1)
        log.info("Scheduled sweep set to: %s (UTC)", cfg.sweep_cron)

    # Enable/disable itself is also checked live inside bazarr_poll.poll_wanted_subtitles()
    # (belt and braces), but the INTERVAL can only change here — APScheduler needs a fresh
    # trigger for that.
    interval = max(1, cfg.poll_new_media_interval_minutes)
    _scheduler.add_job(bazarr_poll.poll_wanted_subtitles, IntervalTrigger(minutes=interval),
                        id=_POLL_JOB_ID, replace_existing=True, max_instances=1)
    log.info("Bazarr wanted-subtitles poll: %s, every %d min",
              "on" if cfg.poll_new_media_enabled else "off", interval)

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


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
