"""Files filters the UI needs: ?run_id=, ?sync_kind=, flag=attention/replacement_failed,
and sorting by lang."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

from verifyarr import db, pipeline
from verifyarr.web.routers import files as files_router


class FilesFilterTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _save(self, name, status, flag, lang="en", reason=None, auto_action=None, run_id=None):
        row = {"lang": lang, "sync_status": status, "correctness_flag": flag,
               "reason": reason, "note": "", "auto_action": auto_action}
        db.update_state(self.conn, Path(f"/m/{name}.mkv"), Path(f"/m/{name}.{lang}.srt"), row,
                        run_id=run_id)
        return self.conn.execute(
            "SELECT id FROM files WHERE subtitle_path = ?", (f"/m/{name}.{lang}.srt",)).fetchone()[0]

    def _total(self, **kw):
        kw.setdefault("page", 1)
        kw.setdefault("page_size", 50)
        return files_router.list_files(user=None, conn=self.conn, **kw)["total"]

    def _run(self):
        run_id = db.create_run(self.conn, "manual_ui", "sweep", False, False)
        db.finish_run(self.conn, run_id, "completed")
        return run_id

    def test_run_id_selects_files_a_job_touched(self):
        r7, r8 = self._run(), self._run()
        self._save("a", "already in sync", "ok", run_id=r7)
        self._save("b", "already in sync", "ok", run_id=r8)
        self._save("c", "already in sync", "ok")  # never touched by a tracked run
        self.assertEqual(self._total(run_id=r7), 1)
        self.assertEqual(self._total(run_id=r8), 1)
        self.assertEqual(self._total(run_id=9999), 0)

    def test_run_id_combines_with_verdict_filters(self):
        r7, r8 = self._run(), self._run()
        self._save("a", "fixed (Δ2.0s)", "ok", run_id=r7)
        self._save("b", "already in sync", "SUSPECT", reason=pipeline.REASON_WRONG_SUBTITLE, run_id=r7)
        self._save("c", "fixed (Δ1.0s)", "ok", run_id=r8)
        # A job page's "Files it changed" / "Files it flagged" links.
        self.assertEqual(self._total(run_id=r7, sync_kind="fixed"), 1)
        self.assertEqual(self._total(run_id=r7, flag="attention"), 1)
        self.assertEqual(self._total(run_id=r8, flag="attention"), 0)

    def test_sync_kind_matches_status_shapes(self):
        self._save("moved", "fixed (Δ12.3s)", "ok")
        self._save("blocks", "fixed (Δ5.0s, 3 sync block(s))", "ok")
        self._save("dry", "would fix (Δ1.0s) [dry-run]", "ok")
        self._save("rate", "fixed (framerate 23.976 -> 24, up to 4.2s)", "ok")
        self._save("stretch", "fixed (rate 25 -> 23.976, up to 90.0s)", "ok")
        self._save("same", "already in sync", "ok")
        self._save("left", "left unchanged (many swapped lines -- not corrected)", "ok")
        self._save("repl", "already in sync", "ok",
                    auto_action="blacklisted in Bazarr (file removed there); remediated: auto-download passed")
        self.assertEqual(self._total(sync_kind="moved"), 3)
        self.assertEqual(self._total(sync_kind="rescaled"), 2)
        self.assertEqual(self._total(sync_kind="fixed"), 5)
        self.assertEqual(self._total(sync_kind="replaced"), 1)
        self.assertEqual(self._total(sync_kind="nochange"), 2)
        self.assertEqual(self._total(sync_kind="unchanged"), 1)

    def test_unknown_sync_kind_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            files_router.list_files(sync_kind="sideways", page=1, page_size=50,
                                    user=None, conn=self.conn)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_flag_attention_covers_suspect_unknown_and_missing(self):
        self._save("a", "already in sync", "SUSPECT", reason=pipeline.REASON_WRONG_SUBTITLE)
        self._save("b", "already in sync", "SUSPECT")  # old row, no reason recorded
        self._save("c", "already in sync", "unknown", reason=pipeline.REASON_NO_SPEECH)
        db.mark_missing(self.conn, Path("/m/d.mkv"), "da")
        self._save("e", "already in sync", "ok")
        self._save("f", "already in sync", "skipped")
        self.assertEqual(self._total(flag="attention"), 4)

    def test_title_matches_exactly(self):
        # series_or_movie_title comes from the video path (movie titles from the parent
        # folder), so the two videos need their own folders to get different titles.
        db.update_state(self.conn, Path("/m/It (2017)/It (2017).mkv"),
                        Path("/m/It (2017)/It (2017).en.srt"),
                        {"lang": "en", "sync_status": "already in sync",
                         "correctness_flag": "ok", "note": ""})
        db.update_state(self.conn, Path("/m/With It (2019)/With It (2019).mkv"),
                        Path("/m/With It (2019)/With It (2019).en.srt"),
                        {"lang": "en", "sync_status": "already in sync",
                         "correctness_flag": "ok", "note": ""})
        got = self.conn.execute("SELECT DISTINCT series_or_movie_title FROM files").fetchall()
        titles = sorted(r[0] for r in got)
        self.assertEqual(len(titles), 2)
        self.assertEqual(self._total(title=titles[0]), 1)
        self.assertGreater(self._total(q="It"), 1)

    def test_flag_replacement_failed_needs_marker_and_suspect(self):
        failed = "blacklisted in Bazarr; no candidate passed; original kept, marked wrong"
        self._save("a", "already in sync", "SUSPECT", reason=pipeline.REASON_WRONG_SUBTITLE,
                    auto_action=failed)
        self._save("b", "already in sync", "SUSPECT", reason=pipeline.REASON_WRONG_SUBTITLE,
                    auto_action="none (action=off)")
        self._save("c", "already in sync", "ok", auto_action=failed)  # stale text, not suspect
        self.assertEqual(self._total(flag="replacement_failed"), 1)

    def test_sort_by_lang(self):
        self._save("a", "already in sync", "ok", lang="da")
        self._save("b", "already in sync", "ok", lang="en")
        out = files_router.list_files(sort="lang", page=1, page_size=50, user=None, conn=self.conn)
        self.assertEqual([r["lang"] for r in out["items"]], ["da", "en"])
        out = files_router.list_files(sort="-lang", page=1, page_size=50, user=None, conn=self.conn)
        self.assertEqual([r["lang"] for r in out["items"]], ["en", "da"])


if __name__ == "__main__":
    unittest.main()
