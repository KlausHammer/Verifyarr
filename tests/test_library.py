"""Library grouping (per-title counts) and the runs ?status= filter."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

from verifyarr import db, pipeline
from verifyarr.web.routers import library as library_router, runs as runs_router


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _video(self, name, kind="series", season_episode="S01E01"):
        # Direct insert (replace_library_videos wipes the table, so it can't accumulate).
        self.conn.execute(
            "INSERT INTO library_videos (video_path, media_root, kind, title, season_episode,"
            " has_subtitle, bazarr_matched) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"/m/{name}.mkv", "/m", kind, "Show", season_episode, 1, 1))
        self.conn.commit()

    def _file(self, name, status, flag, reason=None):
        row = {"lang": "en", "sync_status": status, "correctness_flag": flag,
               "reason": reason, "note": ""}
        db.update_state(self.conn, Path(f"/m/{name}.mkv"), Path(f"/m/{name}.en.srt"), row)

    def test_suspect_count_covers_suspect_and_unknown(self):
        self._video("a")
        self._video("b", season_episode="S01E02")
        self._video("c", season_episode="S01E03")
        self._video("d", season_episode="S01E04")
        self._file("a", "already in sync", "ok")
        self._file("b", "already in sync", "SUSPECT", pipeline.REASON_WRONG_SUBTITLE)
        self._file("c", "already in sync", "unknown", pipeline.REASON_NO_SPEECH)
        self._file("d", "fixed (Δ2.0s)", "ok")
        out = library_router.list_library(kind="series", user=None, conn=self.conn)
        self.assertEqual(len(out["items"]), 1)
        entry = out["items"][0]
        self.assertEqual(entry["video_count"], 4)
        self.assertEqual(entry["ok_count"], 2)
        self.assertEqual(entry["suspect_count"], 2)  # SUSPECT + unknown
        self.assertEqual(entry["missing_count"], 0)


class RunsFilterTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _run(self, status):
        run_id = db.create_run(self.conn, "manual_ui", "sweep", False, False)
        if status != "running":
            db.finish_run(self.conn, run_id, status)
        return run_id

    def test_status_filter(self):
        self._run("completed")
        self._run("failed")
        running = self._run("running")
        def total(status=None):
            return runs_router.list_runs(page=1, page_size=30, status=status,
                                         user=None, conn=self.conn)
        try:
            self.assertEqual(total("failed")["total"], 1)
            out = total("running")
            self.assertEqual([r["id"] for r in out["items"]], [running])
            self.assertEqual(total()["total"], 3)
            with self.assertRaises(HTTPException) as ctx:
                total("bogus")
            self.assertEqual(ctx.exception.status_code, 422)
        finally:
            db.finish_run(self.conn, running, "cancelled")


if __name__ == "__main__":
    unittest.main()
