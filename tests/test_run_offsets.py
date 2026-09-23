"""A block remainder: a run of anchors that agree with each other but not the file.

A repair that settled most of a block leaves the rest 1.5-2.5s off -- under every
per-anchor threshold and without a step big enough to count as a boundary.
"""
from __future__ import annotations

import random
import unittest

from verifyarr.correctness import anchor_run_offsets


def _s(shifts, gap=20.0):
    return [{"start": gap * i, "anchor": None if sh is None else {"shift": sh}}
            for i, sh in enumerate(shifts)]


class RunOffsetTests(unittest.TestCase):
    def test_jitter_averages_out(self):
        r = random.Random(3)
        self.assertEqual(anchor_run_offsets(_s([r.gauss(0, 0.6) for _ in range(60)])), [])

    def test_uniform_offset_is_the_reference_not_a_run(self):
        self.assertEqual(anchor_run_offsets(_s([-45.0 + 0.1 * (i % 3) for i in range(40)])), [])

    def test_remainder_of_a_block_is_found(self):
        # C_S02E02 piecewise full after its resync: 12 anchors at +1.6s, the rest at 0.
        shifts = [0.1] * 20 + [1.6] * 12 + [0.0] * 20
        runs = anchor_run_offsets(_s(shifts))
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["dev"], 1.5, delta=0.2)
        # The span is the union of qualifying windows: it covers the remainder (400-620s)
        # and may reach up to half a window past it.
        self.assertLessEqual(runs[0]["from"], 400.0)
        self.assertGreaterEqual(runs[0]["to"], 620.0)

    def test_short_island_is_not_a_run(self):
        # SH_S01E04's three anchors at -3.4s: fewer than k, so no verdict from this rule.
        self.assertEqual(anchor_run_offsets(_s([0.0] * 30 + [-3.4] * 3 + [-0.3] * 30)), [])

    def test_small_remainder_under_the_bar_is_not_a_run(self):
        self.assertEqual(anchor_run_offsets(_s([0.0] * 20 + [0.8] * 15 + [0.0] * 20)), [])

    def test_too_few_anchors_is_not_a_verdict(self):
        self.assertEqual(anchor_run_offsets(_s([0.0, 2.0, 2.0, 2.0, 0.0])), [])


if __name__ == "__main__":
    unittest.main()
