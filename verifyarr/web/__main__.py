"""Container entrypoint — `python3 -m verifyarr.web`. Makes /data subfolders, starts uvicorn."""

from __future__ import annotations

import os

import uvicorn

from verifyarr import log
from verifyarr.settings import DATA_DIR, DEFAULT_BACKUP_DIR, DEFAULT_REPORT_DIR, DEFAULT_QUARANTINE_DIR

def port_from_env() -> int:
    """The webapp's listen port, from $PORT (set in docker-compose). No built-in default:
    a missing or invalid value stops the container with a clear message."""
    raw = (os.environ.get("PORT") or "").strip()
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise SystemExit(f"PORT must be set to a port number (1-65535), got {raw!r}. "
                         "Set it in docker-compose.yml, same as the right side of ports:.")
    return port


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
