"""Dashboard data: summary_stats' health buckets (+ by_kind ok/fixed) and the next
scheduled-sweep time behind GET /api/runs/next."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, pipeline, scheduler
from verifyarr.web.routers import runs as runs_router


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _save(self, name, status, flag, reason=None):
        row = {"lang": "en", "sync_status": status, "correctness_flag": flag,
               "reason": reason, "note": ""}
        db.update_state(self.conn, Path(f"/m/{name}.mkv"), Path(f"/m/{name}.en.srt"), row)

    def test_health_buckets_match_verdict_precedence(self):
        self._save("a", "already in sync", "ok")
        self._save("b", "fixed (Δ2.0s)", "ok")
        self._save("c", "fixed (framerate 23.976 -> 24, up to 4.0s)", "ok")
        self._save("d", "already in sync", "SUSPECT", pipeline.REASON_WRONG_SUBTITLE)
        self._save("e", "already in sync", "unknown", pipeline.REASON_NO_SPEECH)
        self._save("f", "already in sync", "generated")
        self._save("g", "already in sync", "skipped")
        self._save("h", "error: alass crashed", None)
        db.mark_missing(self.conn, Path("/m/i.mkv"), "da")
        health = db.summary_stats(self.conn)["health"]
        self.assertEqual(health["videos"], 9)
        self.assertEqual(health["insync"], 1)
        self.assertEqual(health["fixed"], 2)
        self.assertEqual(health["suspect"], 1)
        self.assertEqual(health["unknown"], 1)
        self.assertEqual(health["generated"], 1)
        self.assertEqual(health["skipped"], 1)
        self.assertEqual(health["missing"], 1)
        self.assertEqual(health["other"], 1)  # the sync error, nothing else fits
        buckets = sum(health[k] for k in
                      ("insync", "fixed", "suspect", "unknown", "generated",
                       "skipped", "missing", "other"))
        self.assertEqual(buckets, db.summary_stats(self.conn)["files"]["total"])

    def test_by_kind_carries_ok_and_fixed(self):
        self._save("a", "already in sync", "ok")
        self._save("b", "fixed (Δ2.0s)", "ok")
        self._save("c", "already in sync", "SUSPECT", pipeline.REASON_WRONG_SUBTITLE)
        kinds = {r["kind"]: r for r in db.summary_stats(self.conn)["by_kind"]}
        # No library cache rows in this DB, so everything joins as 'unknown'.
        self.assertEqual(kinds["unknown"]["ok"], 1)
        self.assertEqual(kinds["unknown"]["fixed"], 1)
        self.assertEqual(kinds["unknown"]["suspect"], 1)

    def test_next_sweep_at_follows_cron(self):
        now = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)  # a Wednesday
        daily = datetime.fromisoformat(scheduler.next_sweep_at("0 3 * * *", now))
        self.assertEqual((daily.hour, daily.minute), (3, 0))  # local time, as the user set it
        self.assertGreater(daily, now)
        # Crontab 0 is Sunday; APScheduler's own 0 is Monday.
        for expr in ("0 4 * * 0", "0 4 * * 7", "0 4 * * sun"):
            self.assertEqual(datetime.fromisoformat(scheduler.next_sweep_at(expr, now)).strftime("%A %H"),
                             "Sunday 04", expr)
        self.assertEqual(scheduler._cron_dow("1-5"), "mon-fri")
        self.assertIsNone(scheduler.next_sweep_at("not a cron", now))

    def test_next_run_endpoint_reads_saved_cron(self):
        db.set_setting_raw(self.conn, "scheduling.cron", "0 3 * * *")
        out = runs_router.next_run(user=None, conn=self.conn)
        nxt = datetime.fromisoformat(out["next_run_at"])
        self.assertGreater(nxt, datetime.now(timezone.utc))
        self.assertEqual((nxt.hour, nxt.minute), (3, 0))


if __name__ == "__main__":
    unittest.main()
