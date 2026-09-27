"""files.reason: why a file is SUSPECT, stored, filtered and counted for the UI."""
from __future__ import annotations

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

    def test_a_file_that_passes_again_loses_its_reason(self):
        self._save("a", "SUSPECT", pipeline.REASON_MISSING_LINES)
        self._save("a", "ok", pipeline.REASON_MISSING_LINES)  # stale key on the row
        self.assertEqual(db.attention_counts(self.conn), {})
        row = self.conn.execute("SELECT reason FROM files").fetchone()
        self.assertIsNone(row["reason"])


if __name__ == "__main__":
    unittest.main()
