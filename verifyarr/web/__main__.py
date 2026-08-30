"""Container entrypoint — `python3 -m verifyarr.web`. Creates the /data subfolders and
starts uvicorn directly."""

from __future__ import annotations

import uvicorn

from verifyarr import log
from verifyarr.settings import DATA_DIR, DEFAULT_BACKUP_DIR, DEFAULT_REPORT_DIR, DEFAULT_QUARANTINE_DIR


def main() -> None:
    for d in (DATA_DIR, DEFAULT_BACKUP_DIR, DEFAULT_REPORT_DIR, DEFAULT_QUARANTINE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    log.info("verifyarr webapp starting on :8787 (data: %s)", DATA_DIR)
    uvicorn.run("verifyarr.web.app:app", host="0.0.0.0", port=8787, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
