"""App-wide log capture for Settings -> Log's viewer — the same "verifyarr" logger every module
already logs through (see verifyarr/__init__.py), persisted to db.app_log_lines so it survives
past terminal/docker-logs scrollback and can be browsed in the UI. Independent of jobs._RunLogHandler
in jobs.py, which is per-run and only attached while that one job executes — this one is attached
once at startup and stays attached for the life of the process."""

from __future__ import annotations

import logging
import threading

from verifyarr import db


class _AppLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._conn = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                if self._conn is None:
                    self._conn = db.connect()
                db.add_app_log_line(self._conn, record.levelname, self.format(record))
        except Exception:
            pass


_handler = _AppLogHandler()


def install() -> None:
    """Idempotent — safe to call more than once (e.g. dev auto-reload)."""
    logger = logging.getLogger("verifyarr")
    if _handler not in logger.handlers:
        logger.addHandler(_handler)
