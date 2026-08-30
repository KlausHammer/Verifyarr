"""FastAPI app — wires up all routers, initializes the database (incl. the one-time env
import) and starts the scheduler on startup, and serves the built React SPA
(verifyarr/web/static, see Dockerfile) with client-side-routing fallback. The actual server
start happens in `verifyarr.web.__main__`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from verifyarr import applog
from verifyarr import log
from verifyarr import db
from verifyarr import jobs
from verifyarr import scheduler
from verifyarr import settings as settings_mod
from verifyarr.web.routers import auth, settings as settings_router, files, runs, stats, quarantine, bazarr, browse, library, logs

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    applog.install()
    conn = db.connect()
    try:
        settings_mod.migrate_log_level_once(conn)
        migrated = settings_mod.import_from_env_once(conn)
        if migrated:
            log.info("Settings imported from environment variables (one-time migration)")
        cfg = settings_mod.Config.from_db(conn)
    finally:
        conn.close()

    logging.getLogger("verifyarr").setLevel(cfg.log_level)
    scheduler.start()

    if cfg.run_on_start:
        try:
            jobs.runner.start_sweep(trigger="scheduled", force=False)
            log.info("Running an initial sweep now (scheduling.run_on_start=true)")
        except jobs.RunAlreadyActive:
            pass

    yield
    scheduler.shutdown()


app = FastAPI(title="verifyarr", lifespan=lifespan)

for router in (auth.router, settings_router.router, files.router, runs.router, stats.router,
               quarantine.router, bazarr.router, browse.router, library.router, logs.router):
    app.include_router(router)


@app.get("/api/health")
def health():
    return {"ok": True}


if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
else:
    log.warning("No built frontend found at %s — serving API only (run 'npm run build' in frontend/)", STATIC_DIR)
