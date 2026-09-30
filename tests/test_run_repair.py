"""A run of lines at another offset is shifted by its own offset, and only that run."""
import unittest

import pysubs2

from verifyarr import pipeline


def _subs(n=10):
    s = pysubs2.SSAFile()
    for i in range(n):
        s.append(pysubs2.SSAEvent(start=i * 10000, end=i * 10000 + 2000, text=f"l{i}"))
    return s


class RunRepairPlanTests(unittest.TestCase):
    def test_tail_run_shifts_to_the_end_and_nothing_else(self):
        subs = _subs()
        run = {"from": 72.0, "to": 95.0, "dev": 2.5, "n": 4, "cue_from": 70.0, "cue_to": 90.0}
        plan = pipeline._run_repair_plan(subs, [run], last_audio_s=96.0)
        fixed = pipeline.apply_anchor_resync(subs, plan)
        got = [e.start / 1000 for e in fixed.events]
        self.assertEqual(got[:7], [0, 10, 20, 30, 40, 50, 60])
        self.assertEqual(got[7:], [72.5, 82.5, 92.5])

    def test_middle_run_leaves_both_sides(self):
        subs = _subs()
        run = {"from": 30.0, "to": 50.0, "dev": -3.0, "n": 3, "cue_from": 30.0, "cue_to": 50.0}
        plan = pipeline._run_repair_plan(subs, [run], last_audio_s=120.0)
        got = [e.start / 1000 for e in pipeline.apply_anchor_resync(subs, plan).events]
        self.assertEqual(got, [0, 10, 20, 27, 37, 47, 60, 70, 80, 90])

    def test_overlapping_runs_give_no_plan(self):
        subs = _subs()
        a = {"from": 30.0, "to": 60.0, "dev": 2.0, "n": 3, "cue_from": 30.0, "cue_to": 60.0}
        b = {"from": 50.0, "to": 80.0, "dev": 4.0, "n": 3, "cue_from": 50.0, "cue_to": 80.0}
        self.assertEqual(pipeline._run_repair_plan(subs, [a, b], 120.0), [])


if __name__ == "__main__":
    unittest.main()
