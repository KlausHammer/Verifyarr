"""Real flawed library episodes (Z5_flaggede) through the matrix harness: what the pipeline does with each.
Needs the staging data on the Windows side; skipped when it is not there."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
DATA_ROOT = Path(os.environ.get("VERIFYARR_TEST_DATA", "/mnt/c/Users/knham/Desktop/undertekst auto"))
Z = DATA_ROOT / "Z5_flaggede"
CASES = ["COMM_S03E20", "BKLN_S01E02", "EARL_S03E13", "SWAT_S02E12", "TASK_S06E02"]
OUT = "realcases_test"


@unittest.skipUnless((Z / "meta.json").exists(), "Z5_flaggede data not available")
class RealCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cmd = [sys.executable, str(HERE / "e2e_matrix.py"), "--only", ",".join(CASES),
               "--models", "tiny.en-greedy-cpu", "--scenarios", "clean", "--mode", "full",
               "--audio-confirm", "off", "--out", OUT, "--shard", "0", "--redo"]
        subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=900)
        out = HERE / f"{OUT}_0.jsonl"
        cls.rows = {}
        if out.exists():
            cls.rows = {r["slug"]: r for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
        for f in HERE.glob(f"{OUT}_*"):
            f.unlink()

    def row(self, slug):
        self.assertIn(slug, self.rows)
        return self.rows[slug]

    def test_taskmaster_flat_offset_is_a_constant_shift_not_a_rate(self):
        r = self.row("TASK_S06E02")
        self.assertEqual(r["flag"], "ok")
        self.assertIn("fixed (Δ4.0s", r["sync"])
        self.assertNotIn("rate", r["sync"])

    def test_brooklyn_drift_gets_a_rate_fix(self):
        r = self.row("BKLN_S01E02")
        self.assertEqual(r["flag"], "ok")
        self.assertIn("rate stretch +0.3", r["sync"])

    def test_community_blocks_are_re_timed_from_anchors(self):
        r = self.row("COMM_S03E20")
        self.assertIn("anchor region(s)", r["sync"])

    def test_swat_already_in_sync_is_not_damaged(self):
        r = self.row("SWAT_S02E12")
        self.assertEqual(r["flag"], "ok")
        self.assertTrue("Δ0." in r["sync"] or "already in sync" in r["sync"])

    def test_earl_wrong_subtitle_is_flagged_and_untouched(self):
        r = self.row("EARL_S03E13")
        self.assertEqual(r["reason"], "wrong_subtitle")
        self.assertTrue(r["untouched"])


if __name__ == "__main__":
    unittest.main()
