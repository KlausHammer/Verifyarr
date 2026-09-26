"""One run at a time across processes (the Bazarr hook is its own process)."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, jobs


class RunLockTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.lock = Path(self.td.name) / "run.lock"
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_single_gives_up_while_another_run_holds_the_lock(self):
        run_id = jobs.create_run(self.conn, "cli_single", "single", False, False)
        with mock.patch.object(jobs, "RUN_LOCK_PATH", self.lock), \
                mock.patch.object(jobs, "SINGLE_LOCK_WAIT_S", 0.2), \
                mock.patch.object(jobs, "_effective_cfg", side_effect=lambda c, t: c), \
                mock.patch.object(jobs, "_run_single") as single:
            with jobs.run_lock(wait_s=0):
                jobs.execute_run(run_id, mock.Mock(), self.conn, threading.Event(), "single",
                                 trigger="cli_single")
        single.assert_not_called()
        row = self.conn.execute("SELECT status, error_message FROM runs WHERE id=?",
                                (run_id,)).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("another run", row[1])

    def test_unwritable_lock_dir_runs_unlocked(self):
        with mock.patch.object(jobs, "RUN_LOCK_PATH", Path("/proc/nope/run.lock")):
            with jobs.run_lock(wait_s=0):
                pass

    def test_run_proceeds_when_free(self):
        run_id = jobs.create_run(self.conn, "cli_single", "single", False, False)
        with mock.patch.object(jobs, "RUN_LOCK_PATH", self.lock), \
                mock.patch.object(jobs, "_effective_cfg", side_effect=lambda c, t: c), \
                mock.patch.object(jobs, "_run_single") as single:
            jobs.execute_run(run_id, mock.Mock(), self.conn, threading.Event(), "single",
                             trigger="cli_single")
        single.assert_called_once()


if __name__ == "__main__":
    unittest.main()
