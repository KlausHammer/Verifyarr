"""A rewrite that leaves the file further from the audio than the original is undone."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pysubs2
from verifyarr import pipeline as P

CFG = type("C", (), {"dry_run": False})()


def _subs(offset):
    s = pysubs2.SSAFile()
    s.append(pysubs2.SSAEvent(start=int(offset * 1000), end=int(offset * 1000) + 1000, text="x"))
    return s


def _pool_for(share_bad):
    """Pool measuring each file by the start of its only cue: that share of 60 lines sits 10s off."""
    def dense(conn, video, subs, cfg):
        share = share_bad[subs[0].start / 1000.0]
        return [(30.0 + i, 30.0 + i - (10.0 if i < share * 60 else 0.0)) for i in range(60)]
    return dense


def _run(before_share, after_share, status="fixed (Δ5.0s)"):
    original, written = _subs(1.0), _subs(2.0)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "s.srt"
        written.save(str(path))
        row = {"sync_status": status, "note": "", "correctness_flag": "ok"}
        with mock.patch.object(P, "_dense_pool", side_effect=_pool_for({1.0: before_share, 2.0: after_share})):
            P._undo_rewrite_that_made_it_worse(None, Path("v.mkv"), path, CFG, row, original)
        return row, pysubs2.load(str(path))[0].start


class UndoWorseRewriteTests(unittest.TestCase):
    def test_ruined_file_is_restored(self):
        row, start = _run(0.05, 1.0)
        self.assertEqual(start, 1000)
        self.assertTrue(row["sync_status"].startswith("left unchanged"))

    def test_verdict_is_left_as_the_run_reached_it(self):
        # A cut version or jittered cues stay SUSPECT: undoing is not a clean bill of health.
        for flag in ("ok", "SUSPECT"):
            tmp_row = {"correctness_flag": flag, "reason": "partly_out_of_sync" if flag == "SUSPECT" else None}
            original, written = _subs(1.0), _subs(2.0)
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "s.srt"
                written.save(str(path))
                row = {"sync_status": "fixed (Δ5.0s)", "note": "", **tmp_row}
                with mock.patch.object(P, "_dense_pool", side_effect=_pool_for({1.0: 0.05, 2.0: 1.0})):
                    P._undo_rewrite_that_made_it_worse(None, Path("v.mkv"), path, CFG, row, original)
            self.assertEqual((row["correctness_flag"], row["reason"]), (tmp_row["correctness_flag"], tmp_row["reason"]))

    def test_real_improvement_is_kept(self):
        row, start = _run(0.8, 0.05)
        self.assertEqual(start, 2000)
        self.assertTrue(row["sync_status"].startswith("fixed"))

    def test_original_that_matches_only_early_is_compared_on_that_stretch(self):
        # A drifted original matches only the first part of the file; the fix matches the whole
        # file, including a noisy late half. Compared on the early stretch both are clean.
        def dense(conn, video, subs, cfg):
            early = [(30.0 + i, 30.0 + i) for i in range(60)]
            if subs[0].start == 1000:
                return early
            return early + [(500.0 + i, 500.0 + i - (10.0 if i % 2 else 0.0)) for i in range(60)]
        original, written = _subs(1.0), _subs(2.0)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.srt"
            written.save(str(path))
            row = {"sync_status": "fixed (rate 23.976/25, up to 5.0s)", "note": ""}
            with mock.patch.object(P, "_dense_pool", side_effect=dense):
                P._undo_rewrite_that_made_it_worse(None, Path("v.mkv"), path, CFG, row, original)
            self.assertEqual(pysubs2.load(str(path))[0].start, 2000)
            self.assertTrue(row["sync_status"].startswith("fixed"))

    def test_untouched_file_is_ignored(self):
        row, start = _run(0.05, 1.0, status="already in sync")
        self.assertEqual(start, 2000)


if __name__ == "__main__":
    unittest.main()
