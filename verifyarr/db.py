"""verifyarr.db — schema and data layer for `/data/verifyarr.db`. Shared by the CLI
(`verifyarr.cli`) and the webapp (`verifyarr.web`).

No ORM — the schema is small and fully known, raw `sqlite3` matches the rest of the
codebase's style. `connect()` opens a NEW connection every time instead of sharing one
global — sqlite3 connections aren't thread-safe, and both the API thread and the background
job thread touch the database at once. WAL mode keeps that cheap (a short writer doesn't
block concurrent readers)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    subtitle_path         TEXT,
    video_path            TEXT NOT NULL,
    lang                  TEXT,
    media_root            TEXT,
    season_episode        TEXT,
    series_or_movie_title TEXT,
    video_mtime REAL, video_size INTEGER, subtitle_mtime REAL, subtitle_size INTEGER,
    last_processed        TEXT,
    sync_status            TEXT,
    sync_max_shift_s       REAL,
    structural_change      INTEGER,
    sync_split_blocks      INTEGER,
    correctness_flag       TEXT,
    correctness_avg_score  REAL,
    line_order_fixed       INTEGER,  -- see line_order.py. NULL = not checked (feature off),
    line_order_flagged     INTEGER,  -- 0 = checked and nothing found, >0 = counts from last run.
    line_order_cache_key   TEXT,     -- line_order.cache_key_for() this cache was collected under;
    line_order_cache_json  TEXT,     -- collect_samples() result -- reused across runs while the
                                      -- subtitle is unchanged, regardless of whether line-order is
                                      -- even turned on (see pipeline.py, line_order.py docstring).
    note                   TEXT,
    auto_action            TEXT,
    last_run_id INTEGER REFERENCES runs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_subtitle_path ON files(subtitle_path) WHERE subtitle_path IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_missing ON files(video_path, lang) WHERE subtitle_path IS NULL;
CREATE INDEX IF NOT EXISTS ix_files_flag ON files(correctness_flag);
CREATE INDEX IF NOT EXISTS ix_files_lang ON files(lang);
CREATE INDEX IF NOT EXISTS ix_files_status ON files(sync_status);

CREATE TABLE IF NOT EXISTS correctness_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subtitle_path TEXT NOT NULL,
    video_path TEXT,
    lang TEXT,
    checked_at TEXT NOT NULL,
    correctness_flag TEXT,
    correctness_avg_score REAL,
    audio_lang TEXT,
    samples_json TEXT,
    run_id INTEGER REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS ix_ch_path_time ON correctness_history(subtitle_path, checked_at);
CREATE INDEX IF NOT EXISTS ix_ch_time ON correctness_history(checked_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_total INTEGER,
    files_processed INTEGER DEFAULT 0,
    files_changed INTEGER DEFAULT 0,
    files_suspect INTEGER DEFAULT 0,
    files_error INTEGER DEFAULT 0,
    dry_run INTEGER DEFAULT 0,
    force INTEGER DEFAULT 0,
    error_message TEXT,
    target_kind  TEXT,  -- 'movie' | 'series' | NULL — see Activity's Type/Target columns
    target_title TEXT   -- set for a single-file/single-title run; NULL for a whole-library sweep
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_single_running ON runs(status) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS run_log_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rll_run ON run_log_lines(run_id, id);

-- The whole app's log, not just one run's (see run_log_lines above for that) -- what Settings ->
-- Log's viewer reads from. Fed by applog.py's handler, trimmed to the last APP_LOG_MAX_ROWS lines
-- on insert so this never grows without bound.
CREATE TABLE IF NOT EXISTS app_log_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    password_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS blacklist_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subtitle_path TEXT,
    video_path TEXT,
    kind TEXT,
    provider TEXT,
    subs_id TEXT,
    language TEXT,
    series_id TEXT,
    episode_id TEXT,
    radarr_id TEXT,
    blacklisted_at TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    remediation_outcome TEXT
);
CREATE INDEX IF NOT EXISTS ix_bl_time ON blacklist_actions(blacklisted_at);

-- Cached result of the last library scan (see web/routers/library.py) — GET /api/library
-- reads ONLY from here (no live filesystem scan per page load). Filled by either a sweep
-- (which already walked the tree anyway) or the separate, fast POST /api/library/rescan
-- call (same discovery, without the sync/correctness work) — see jobs._run_sweep and
-- web/routers/library.py.
CREATE TABLE IF NOT EXISTS library_videos (
    video_path      TEXT PRIMARY KEY,
    media_root      TEXT,
    kind            TEXT NOT NULL DEFAULT 'series',  -- 'movie' | 'series', see Config.kind_for
    title           TEXT NOT NULL,
    season_episode  TEXT,
    has_subtitle    INTEGER NOT NULL DEFAULT 0,
    video_mtime          REAL,     -- see get_persisted_embedded_cache
    video_size            INTEGER,
    embedded_langs_json  TEXT,     -- NULL = never embedded-checked (had full external coverage)
    bazarr_matched INTEGER NOT NULL DEFAULT 0  -- title came from Bazarr, not a filename/folder guess
);
CREATE INDEX IF NOT EXISTS ix_library_videos_title ON library_videos(title);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Opens a new connection. `path` is optional only to make unit testing easier — normal
    use is `db.connect()` with no arguments, which uses the fixed path from
    settings.DEFAULT_DB_PATH (imported locally to avoid a circular import)."""
    if path is None:
        from verifyarr.settings import DEFAULT_DB_PATH
        path = DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI's sync dependency generators (Depends(get_conn)) run their
    # __enter__ and __exit__ halves via anyio's threadpool, which does NOT guarantee the same
    # worker thread for both halves of one request — sqlite3's default same-thread check then
    # raises "SQLite objects created in a thread can only be used in that same thread" on close(),
    # intermittently, whenever the pool happens to hand the two halves to different threads.
    # Safe here because each connection is still only ever used by ONE logical request/job at a
    # time, never concurrently from two threads at once.
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    # Light, non-destructive column migrations for fields added AFTER a table already
    # existed for a user — "CREATE TABLE IF NOT EXISTS" alone adds nothing to an existing
    # table. library_videos is pure cache (see replace_library_videos), so no data is lost
    # by the column just showing up empty/default at the next (re)scan.
    try:
        conn.execute("ALTER TABLE library_videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'series'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS ix_library_videos_kind ON library_videos(kind)")
    for col, coltype in (("video_mtime", "REAL"), ("video_size", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE library_videos ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    try:
        conn.execute("ALTER TABLE library_videos ADD COLUMN embedded_langs_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE library_videos ADD COLUMN bazarr_matched INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    for col in ("line_order_fixed", "line_order_flagged"):
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    for col in ("line_order_cache_key", "line_order_cache_json"):
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    for col in ("target_kind", "target_title"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def init_db(path: Path) -> sqlite3.Connection:
    """Bevaret navn for bagudkompatibilitet med `verifyarr.cli` — svarer nu til `connect(path)`."""
    return connect(path)


# --- files (sync/correctness status per video+subtitle+language) --------------------------------

def should_skip(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path) -> bool:
    row = conn.execute(
        "SELECT video_mtime, video_size, subtitle_mtime, subtitle_size FROM files WHERE subtitle_path = ?",
        (str(subtitle_path),),
    ).fetchone()
    if not row:
        return False
    try:
        vstat, sstat = video_path.stat(), subtitle_path.stat()
    except OSError:
        return False
    return (row["video_mtime"] == vstat.st_mtime and row["video_size"] == vstat.st_size
            and row["subtitle_mtime"] == sstat.st_mtime and row["subtitle_size"] == sstat.st_size)


def _infer_title_and_episode(video_path: Path, media_root: Optional[Path] = None):
    from verifyarr.discovery import infer_title_and_episode
    return infer_title_and_episode(video_path, media_root)


def update_state(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path, row: dict,
                  run_id: Optional[int] = None, media_root: Optional[Path] = None) -> None:
    """Upserts one files row (keyed on subtitle_path) with the result of process_pair, and
    appends a correctness_history row if a correctness check actually ran (flag 'ok' or
    'SUSPECT') — drives the match-rate graph. Note: subtitle_path may have since been moved
    to quarantine (see handle_suspect in pipeline.py) — we still write the last known status
    to the ORIGINAL path; a stat() failure on either video or subtitle doesn't stop the
    status from being saved."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        vstat = video_path.stat()
        video_mtime, video_size = vstat.st_mtime, vstat.st_size
    except OSError:
        video_mtime = video_size = None
    try:
        sstat = subtitle_path.stat()
        subtitle_mtime, subtitle_size = sstat.st_mtime, sstat.st_size
    except OSError:
        subtitle_mtime = subtitle_size = None

    season_episode, title = _infer_title_and_episode(video_path, media_root)
    conn.execute("""
        INSERT INTO files (subtitle_path, video_path, lang, media_root, season_episode,
                            series_or_movie_title, video_mtime, video_size, subtitle_mtime,
                            subtitle_size, last_processed, sync_status, sync_max_shift_s,
                            structural_change, sync_split_blocks, correctness_flag,
                            correctness_avg_score, line_order_fixed, line_order_flagged,
                            line_order_cache_key, line_order_cache_json,
                            note, auto_action, last_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subtitle_path) WHERE subtitle_path IS NOT NULL DO UPDATE SET
            video_path=excluded.video_path, lang=excluded.lang, media_root=excluded.media_root,
            season_episode=excluded.season_episode, series_or_movie_title=excluded.series_or_movie_title,
            video_mtime=excluded.video_mtime, video_size=excluded.video_size,
            subtitle_mtime=excluded.subtitle_mtime, subtitle_size=excluded.subtitle_size,
            last_processed=excluded.last_processed, sync_status=excluded.sync_status,
            sync_max_shift_s=excluded.sync_max_shift_s, structural_change=excluded.structural_change,
            sync_split_blocks=excluded.sync_split_blocks, correctness_flag=excluded.correctness_flag,
            correctness_avg_score=excluded.correctness_avg_score,
            line_order_fixed=excluded.line_order_fixed, line_order_flagged=excluded.line_order_flagged,
            -- Only overwritten when THIS run actually collected fresh line-order/correctness
            -- data (see pipeline.py) -- a run that skipped it entirely (e.g. correctness turned
            -- off) must not wipe out a still-valid cache from an earlier run.
            line_order_cache_key=COALESCE(excluded.line_order_cache_key, files.line_order_cache_key),
            line_order_cache_json=COALESCE(excluded.line_order_cache_json, files.line_order_cache_json),
            note=excluded.note, auto_action=excluded.auto_action, last_run_id=excluded.last_run_id
    """, (str(subtitle_path), str(video_path), row.get("lang") or None,
          str(media_root) if media_root else str(video_path.parent), season_episode, title,
          video_mtime, video_size, subtitle_mtime, subtitle_size, now,
          row.get("sync_status"), row.get("sync_max_shift_s"), int(bool(row.get("structural_change"))),
          row.get("sync_split_blocks"), row.get("correctness_flag"), row.get("correctness_avg_score"),
          row.get("line_order_fixed"), row.get("line_order_flagged"),
          row.get("line_order_cache_key"), row.get("line_order_cache_json"),
          row.get("note"), row.get("auto_action"), run_id))

    if row.get("correctness_flag") in ("ok", "SUSPECT"):
        samples = row.get("correctness_samples")
        conn.execute("""
            INSERT INTO correctness_history (subtitle_path, video_path, lang, checked_at,
                                              correctness_flag, correctness_avg_score, audio_lang,
                                              samples_json, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(subtitle_path), str(video_path), row.get("lang") or None, now,
              row.get("correctness_flag"), row.get("correctness_avg_score"),
              row.get("correctness_audio_lang"), json.dumps(samples) if samples else None, run_id))
    conn.commit()


def mark_missing(conn: sqlite3.Connection, video_path: Path, lang: str,
                  media_root: Optional[Path] = None) -> None:
    """Records that a wanted language has no subtitle at all for this video (files row with
    subtitle_path=NULL, sync_status='missing') — see discovery.discover_missing."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        vstat = video_path.stat()
        video_mtime, video_size = vstat.st_mtime, vstat.st_size
    except OSError:
        video_mtime = video_size = None
    season_episode, title = _infer_title_and_episode(video_path, media_root)
    conn.execute("""
        INSERT INTO files (subtitle_path, video_path, lang, media_root, season_episode,
                            series_or_movie_title, video_mtime, video_size, last_processed, sync_status)
        VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'missing')
        ON CONFLICT(video_path, lang) WHERE subtitle_path IS NULL DO UPDATE SET
            media_root=excluded.media_root, season_episode=excluded.season_episode,
            series_or_movie_title=excluded.series_or_movie_title, video_mtime=excluded.video_mtime,
            video_size=excluded.video_size, last_processed=excluded.last_processed
    """, (str(video_path), lang, str(media_root) if media_root else None, season_episode, title,
          video_mtime, video_size, now))
    conn.commit()


def get_file(conn: sqlite3.Connection, file_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def get_line_order_cache(conn: sqlite3.Connection, subtitle_path: Path) -> Optional[dict]:
    """The collect_samples() result cached for this subtitle by an earlier run (see pipeline.py,
    line_order.cache_key_for) -- None if there's never been one. Caller still has to check
    "key" against a freshly-computed cache_key_for() before trusting "json"; a stored key from a
    subtitle that has since changed is simply stale, not deleted."""
    row = conn.execute(
        "SELECT line_order_cache_key, line_order_cache_json FROM files WHERE subtitle_path = ?",
        (str(subtitle_path),),
    ).fetchone()
    if not row or not row["line_order_cache_json"]:
        return None
    return {"key": row["line_order_cache_key"], "json": row["line_order_cache_json"]}


def list_files(conn: sqlite3.Connection, q: Optional[str] = None, flag: Optional[str] = None,
               status: Optional[str] = None, lang: Optional[str] = None,
               sort: str = "-last_processed", page: int = 1, page_size: int = 50):
    where, params = [], []
    if q:
        where.append("(video_path LIKE ? OR subtitle_path LIKE ? OR series_or_movie_title LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if flag:
        where.append("correctness_flag = ?")
        params.append(flag)
    if status:
        where.append("sync_status = ?")
        params.append(status)
    if lang:
        where.append("lang = ?")
        params.append(lang)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sort_col = sort.lstrip("-")
    allowed_sort = {"last_processed", "video_path", "correctness_avg_score", "sync_status",
                     "correctness_flag", "season_episode"}
    if sort_col not in allowed_sort:
        sort_col = "last_processed"
    direction = "DESC" if sort.startswith("-") else "ASC"

    total = conn.execute(f"SELECT COUNT(*) FROM files {where_sql}", params).fetchone()[0]
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        f"SELECT * FROM files {where_sql} ORDER BY {sort_col} {direction} NULLS LAST LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


# --- runs (one per sweep/single run) -------------------------------------------------------------

class RunAlreadyActive(Exception):
    """Raised when a new job is started while another is already running (enforced by
    ux_runs_single_running)."""


def create_run(conn: sqlite3.Connection, trigger: str, mode: str, dry_run: bool, force: bool,
                target_kind: Optional[str] = None, target_title: Optional[str] = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute("""
            INSERT INTO runs (trigger, mode, status, started_at, dry_run, force, target_kind, target_title)
            VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
        """, (trigger, mode, now, int(bool(dry_run)), int(bool(force)), target_kind, target_title))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise RunAlreadyActive("a job is already running")


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, error_message: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, error_message = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(), error_message, run_id),
    )
    conn.commit()


def update_run_counts(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    if not fields:
        return
    set_sql = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {set_sql} WHERE id = ?", list(fields.values()) + [run_id])
    conn.commit()


def bump_run_progress(conn: sqlite3.Connection, run_id: int, row: dict) -> None:
    changed = 1 if str(row.get("sync_status", "")).startswith("fixed") else 0
    suspect = 1 if row.get("correctness_flag") == "SUSPECT" else 0
    error = 1 if "error" in str(row.get("sync_status", "")) else 0
    conn.execute("""
        UPDATE runs SET files_processed = files_processed + 1,
                        files_changed = files_changed + ?,
                        files_suspect = files_suspect + ?,
                        files_error = files_error + ?
        WHERE id = ?
    """, (changed, suspect, error, run_id))
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def get_running_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE status = 'running' LIMIT 1").fetchone()


def reconcile_orphaned_run(conn: sqlite3.Connection) -> Optional[int]:
    """Called once at process startup (see web/app.py's lifespan), BEFORE anything else touches
    jobs.runner. A 'running' row found here can only be left over from a previous process that
    was killed or restarted mid-job — a freshly started process's JobRunner always begins with
    no job in flight, so it can never actually still be running. Left alone, an orphaned row
    like this would permanently jam the app: ux_runs_single_running (see create_run) blocks any
    new run from starting at all, and Cancel always fails too (it checks
    jobs.runner.current_run_id(), which this new process never set for a run it never started).
    Returns the reconciled run's id, or None if there wasn't one."""
    row = get_running_run(conn)
    if row is None:
        return None
    finish_run(conn, row["id"], "failed", error_message="Interrupted by a server restart")
    return row["id"]


def list_runs(conn: sqlite3.Connection, page: int = 1, page_size: int = 30):
    total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, offset)
    ).fetchall()
    return rows, total


# --- run_log_lines (structured log lines per run, drives Activity/SSE) ---------------------------

def add_log_line(conn: sqlite3.Connection, run_id: int, level: str, message: str) -> None:
    conn.execute(
        "INSERT INTO run_log_lines (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
        (run_id, datetime.now(timezone.utc).isoformat(), level, message),
    )
    conn.commit()


def list_log_lines(conn: sqlite3.Connection, run_id: int, after_id: int = 0, limit: int = 2000):
    return conn.execute(
        "SELECT * FROM run_log_lines WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after_id, limit),
    ).fetchall()


# --- app_log_lines (whole-app log, see applog.py) -------------------------------------------------

APP_LOG_MAX_ROWS = 5000


def add_app_log_line(conn: sqlite3.Connection, level: str, message: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("INSERT INTO app_log_lines (ts, level, message) VALUES (?, ?, ?)", (now, level, message))
    conn.commit()
    # Trimmed here rather than on every insert -- a DELETE every 20th line is cheap enough that
    # the table never grows far past APP_LOG_MAX_ROWS, without paying the cost on every single one.
    if cur.lastrowid % 20 == 0:
        conn.execute("DELETE FROM app_log_lines WHERE id <= (SELECT MAX(id) FROM app_log_lines) - ?",
                     (APP_LOG_MAX_ROWS,))
        conn.commit()


def list_app_log_lines(conn: sqlite3.Connection, after_id: int = 0, limit: int = 500):
    """after_id=0 -> the most recent `limit` lines, oldest first (ready to render top-to-bottom).
    after_id>0 -> only lines newer than that id (for polling), also oldest first."""
    if after_id:
        return conn.execute(
            "SELECT * FROM app_log_lines WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
    rows = conn.execute("SELECT * FROM app_log_lines ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))


# --- stats ---------------------------------------------------------------------------------------

def match_rate_series(conn: sqlite3.Connection, group_by: str = "day", days: int = 90):
    """Match-rate over tid til Stats-siden — andel 'ok' blandt afsluttede korrekthedstjek,
    grupperet pr. dag eller uge."""
    fmt = "%Y-%m-%d" if group_by == "day" else "%Y-W%W"
    rows = conn.execute(f"""
        SELECT strftime('{fmt}', checked_at) AS period,
               COUNT(*) AS total,
               SUM(CASE WHEN correctness_flag = 'ok' THEN 1 ELSE 0 END) AS ok_count,
               SUM(CASE WHEN correctness_flag = 'SUSPECT' THEN 1 ELSE 0 END) AS suspect_count,
               AVG(correctness_avg_score) AS avg_score
        FROM correctness_history
        WHERE checked_at >= datetime('now', ?)
        GROUP BY period ORDER BY period
    """, (f"-{days} days",)).fetchall()
    return rows


def summary_stats(conn: sqlite3.Connection):
    files_row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN sync_status = 'missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN correctness_flag = 'SUSPECT' THEN 1 ELSE 0 END) AS suspect,
               SUM(CASE WHEN correctness_flag = 'ok' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN sync_status LIKE 'error%' OR sync_status = 'unexpected-error' THEN 1 ELSE 0 END) AS errors,
               SUM(CASE WHEN sync_status LIKE 'fixed%' OR sync_status LIKE 'would fix%' THEN 1 ELSE 0 END) AS out_of_sync,
               SUM(COALESCE(line_order_fixed, 0)) AS line_order_fixed_total,
               SUM(COALESCE(line_order_flagged, 0)) AS line_order_flagged_total
        FROM files
    """).fetchone()
    lang_rows = conn.execute("""
        SELECT lang, AVG(correctness_avg_score) AS avg_score, COUNT(*) AS n
        FROM files WHERE lang IS NOT NULL AND correctness_avg_score IS NOT NULL
        GROUP BY lang ORDER BY lang
    """).fetchall()
    # kind (movie/series) isn't stored on files itself — joined from the library_videos cache
    # (see web/routers/library.py), which is the canonical source for it.
    kind_rows = conn.execute("""
        SELECT COALESCE(lv.kind, 'unknown') AS kind, COUNT(*) AS n,
               SUM(CASE WHEN f.correctness_flag = 'SUSPECT' THEN 1 ELSE 0 END) AS suspect,
               SUM(CASE WHEN f.sync_status = 'missing' THEN 1 ELSE 0 END) AS missing
        FROM files f LEFT JOIN library_videos lv ON lv.video_path = f.video_path
        GROUP BY kind ORDER BY kind
    """).fetchall()
    score_dist_rows = conn.execute("""
        SELECT CASE
                 WHEN correctness_avg_score < 0.2 THEN '0-20%'
                 WHEN correctness_avg_score < 0.4 THEN '20-40%'
                 WHEN correctness_avg_score < 0.6 THEN '40-60%'
                 WHEN correctness_avg_score < 0.8 THEN '60-80%'
                 ELSE '80-100%'
               END AS bucket,
               COUNT(*) AS n
        FROM files WHERE correctness_avg_score IS NOT NULL
        GROUP BY bucket
    """).fetchall()
    last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"files": dict(files_row), "by_lang": [dict(r) for r in lang_rows],
            "by_kind": [dict(r) for r in kind_rows], "score_distribution": [dict(r) for r in score_dist_rows],
            "last_run": dict(last_run) if last_run else None}


# --- blacklist_actions -----------------------------------------------------------------------

def add_blacklist_action(conn: sqlite3.Connection, *, subtitle_path=None, video_path=None, kind=None,
                          provider=None, subs_id=None, language=None, series_id=None, episode_id=None,
                          radarr_id=None, run_id=None, remediation_outcome=None) -> None:
    conn.execute("""
        INSERT INTO blacklist_actions (subtitle_path, video_path, kind, provider, subs_id, language,
                                        series_id, episode_id, radarr_id, blacklisted_at, run_id,
                                        remediation_outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (subtitle_path, video_path, kind, provider, subs_id, language, series_id, episode_id,
          radarr_id, datetime.now(timezone.utc).isoformat(), run_id, remediation_outcome))
    conn.commit()


# --- library cache (see library_videos above) -----------------------------------------------------

def replace_library_videos(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Replaces the WHOLE library_videos table in one transaction — simpler and just as
    correct as a diff (handles deleted files for free), and cheap enough even for a private
    library with thousands of rows. Only called on an actual (re)scan, NEVER per page load.

    video_mtime/video_size/embedded_langs_json (all optional — a row may have None for any of
    them) feed get_persisted_embedded_cache, which lets the SCHEDULED library poll skip
    re-ffprobing a video that hasn't changed since it was last checked. "Detect now" always
    passes fresh values here (it never reuses the persisted cache itself), so a manual click
    still re-checks everything, exactly as before.

    bazarr_matched (optional, defaults False) — whether `title` came from Bazarr's own matched
    data rather than a folder/filename guess (see discovery.build_library_video_rows). Surfaced
    as a "not found in Bazarr" badge on the Library page (see web/routers/library.py) so a
    path-mapping problem or genuinely unmanaged content is visible at a glance instead of only
    showing up as a wrong-looking name."""
    conn.execute("DELETE FROM library_videos")
    conn.executemany(
        "INSERT INTO library_videos (video_path, media_root, kind, title, season_episode, has_subtitle, "
        "video_mtime, video_size, embedded_langs_json, bazarr_matched) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["video_path"], r["media_root"], r["kind"], r["title"], r["season_episode"], int(r["has_subtitle"]),
          r.get("video_mtime"), r.get("video_size"), r.get("embedded_langs_json"), int(r.get("bazarr_matched", False)))
         for r in rows],
    )
    set_setting_raw(conn, "library.last_scanned_at", datetime.now(timezone.utc).isoformat())
    conn.commit()


def get_persisted_embedded_cache(conn: sqlite3.Connection, all_videos: list) -> dict:
    """{video_path: {embedded lang, ...}} recovered from the LAST scan's library_videos row,
    but ONLY for a video whose size+mtime on disk still match what was recorded then — an
    unchanged file's embedded tracks can't have changed either, so there's no need to ffprobe
    it again. Used by the scheduled library poll (poll_library_for_new_media) to make a routine
    automatic check cheap over time, once a video has been seen at least once. "Detect now"
    deliberately does NOT use this — a manual click is expected to always re-check everything
    fresh (see discovery.resolve_embedded_cache's callers)."""
    result: dict = {}
    prior = {
        row["video_path"]: row
        for row in conn.execute(
            "SELECT video_path, video_mtime, video_size, embedded_langs_json FROM library_videos "
            "WHERE embedded_langs_json IS NOT NULL"
        ).fetchall()
    }
    for video in all_videos:
        row = prior.get(str(video))
        if row is None:
            continue
        try:
            st = video.stat()
        except OSError:
            continue
        if row["video_mtime"] == st.st_mtime and row["video_size"] == st.st_size:
            try:
                result[video] = set(json.loads(row["embedded_langs_json"]))
            except (ValueError, TypeError):
                pass
    return result


def list_library_videos(conn: sqlite3.Connection, kind: Optional[str] = None):
    if kind:
        return conn.execute(
            "SELECT * FROM library_videos WHERE kind = ? ORDER BY title COLLATE NOCASE", (kind,)
        ).fetchall()
    return conn.execute("SELECT * FROM library_videos ORDER BY title COLLATE NOCASE").fetchall()


def list_blacklist_actions(conn: sqlite3.Connection, page: int = 1, page_size: int = 50):
    total = conn.execute("SELECT COUNT(*) FROM blacklist_actions").fetchone()[0]
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        "SELECT * FROM blacklist_actions ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, offset)
    ).fetchall()
    return rows, total


# --- settings (flat key/value, see verifyarr.settings for the typed layer on top) ------------------

def get_setting_raw(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_all_settings_raw(conn: sqlite3.Connection) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_setting_raw(conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """, (key, value, now))
    conn.commit()


def set_settings_raw(conn: sqlite3.Connection, values: dict) -> None:
    for k, v in values.items():
        set_setting_raw(conn, k, v)
