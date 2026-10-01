"""Container entrypoint — `python3 -m verifyarr.web`. Makes /data subfolders, starts uvicorn."""

from __future__ import annotations

import os

import uvicorn

from verifyarr import log
from verifyarr.settings import DATA_DIR, DEFAULT_BACKUP_DIR, DEFAULT_REPORT_DIR, DEFAULT_QUARANTINE_DIR

DEFAULT_PORT = 6868


def port_from_env() -> int:
    """The webapp's listen port: $PORT, or 6868. Garbage falls back to 6868 with a
    warning instead of crashing the container on a typo."""
    raw = (os.environ.get("PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if 1 <= port <= 65535:
        return port
    log.warning("Ignoring invalid PORT=%r, listening on %d instead", raw, DEFAULT_PORT)
    return DEFAULT_PORT


def main() -> None:
    # whisper-models: landing spot for a non-baked WHISPER_MODEL on first use.
    for d in (DATA_DIR, DEFAULT_BACKUP_DIR, DEFAULT_REPORT_DIR, DEFAULT_QUARANTINE_DIR,
              DATA_DIR / "whisper-models"):
        d.mkdir(parents=True, exist_ok=True)
    port = port_from_env()
    log.info("verifyarr webapp starting on :%d (data: %s)", port, DATA_DIR)
    uvicorn.run("verifyarr.web.app:app", host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
