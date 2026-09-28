"""files.reason: why a file is SUSPECT, stored, filtered and counted for the UI."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, pipeline
from verifyarr.web.routers import files as files_router, stats as stats_router


class ReasonTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _save(self, name, flag, reason=None):
        row = {"lang": "en", "sync_status": "already in sync", "correctness_flag": flag,
               "reason": reason, "note": ""}
        db.update_state(self.conn, Path(f"/m/{name}.mkv"), Path(f"/m/{name}.en.srt"), row)

    def test_reason_is_stored_filtered_and_counted(self):
        self._save("a", "SUSPECT", pipeline.REASON_PAST_AUDIO_END)
        self._save("b", "SUSPECT", pipeline.REASON_PAST_AUDIO_END)
        self._save("c", "SUSPECT", pipeline.REASON_WRONG_SUBTITLE)
        self._save("d", "SUSPECT")          # flagged before reasons existed
        self._save("e", "ok")
        self.assertEqual(db.attention_counts(self.conn),
                         {"past_audio_end": 2, "wrong_subtitle": 1, "other": 1})
        out = files_router.list_files(reason="past_audio_end", page=1, page_size=50, user=None, conn=self.conn)
        self.assertEqual(out["total"], 2)
        self.assertEqual({r["reason"] for r in out["items"]}, {"past_audio_end"})
        self.assertEqual(files_router.list_files(reason="other", page=1, page_size=50, user=None, conn=self.conn)["total"], 1)
        self.assertEqual(stats_router.attention(user=None, conn=self.conn)["items"][0],
                         {"reason": "past_audio_end", "count": 2})

    def test_unknown_files_carry_a_reason_and_need_attention(self):
        row = {"lang": "en", "sync_status": "already in sync", "note": ""}
        pipeline._flag_unknown(row, [{"error": "VAD silence-skip (<2s speech in window)"},
                                     {"score": None}])
        self.assertEqual(row["reason"], pipeline.REASON_NO_SPEECH)
        db.update_state(self.conn, Path("/m/a.mkv"), Path("/m/a.en.srt"), row)
        row = {"lang": "en", "sync_status": "already in sync", "note": ""}
        pipeline._flag_unknown(row, [{"error": "audio extraction failed"},
                                     {"error": "VAD silence-skip (<2s speech in window)"}])
        self.assertEqual(row["reason"], pipeline.REASON_CHECK_FAILED)
        db.update_state(self.conn, Path("/m/b.mkv"), Path("/m/b.en.srt"), row)
        self.assertEqual(db.attention_counts(self.conn),
                         {"check_failed": 1, "no_speech_heard": 1})
        out = files_router.list_files(reason="check_failed", page=1, page_size=50,
                                      user=None, conn=self.conn)
        self.assertEqual(out["total"], 1)

    def test_unknown_reason_is_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            files_router.list_files(reason="wrong_subtitel", page=1, page_size=50,
                                    user=None, conn=self.conn)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_old_database_gets_the_reason_column(self):
        path = Path(self.td.name) / "old.db"
        db.connect(path).close()
        c = sqlite3.connect(path)
        c.execute("ALTER TABLE files DROP COLUMN reason")
        c.commit()
        c.close()
        conn = db.connect(path)
        self.assertIn("reason", {r[1] for r in conn.execute("PRAGMA table_info(files)")})
        conn.close()

    def test_a_file_that_passes_again_loses_its_reason(self):
        self._save("a", "SUSPECT", pipeline.REASON_MISSING_LINES)
        self._save("a", "ok", pipeline.REASON_MISSING_LINES)  # stale key on the row
        self.assertEqual(db.attention_counts(self.conn), {})
        row = self.conn.execute("SELECT reason FROM files").fetchone()
        self.assertIsNone(row["reason"])


if __name__ == "__main__":
    unittest.main()
