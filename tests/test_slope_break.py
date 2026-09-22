"""Block boundaries that survive a repair, seen as a step instead of a count.

A half-repaired file anchors as densely as a healthy one (measured: 76% vs 77% of
samples), so counting missing or bad anchors cannot find it -- the stretch still
wrong anchors fine against its own displaced text. Speech plays in one direction,
so the audio->cue mapping must run at slope 1; a step in it is the unresolved
boundary.
"""
from __future__ import annotations

import unittest

from verifyarr.correctness import anchor_slope_breaks


def _s(*pairs):
    """(start, shift) -> sample dicts; shift None means the clip produced no anchor."""
    return [{"start": t, "anchor": None if sh is None else {"shift": sh}} for t, sh in pairs]


class SlopeBreakTests(unittest.TestCase):
    def test_flat_mapping_is_clean(self):
        self.assertEqual(anchor_slope_breaks(_s((0, 0.2), (20, 0.1), (40, 0.3))), [])

    def test_uniform_offset_is_clean(self):
        # A whole-file shift moves every anchor the same way: no step between them.
        self.assertEqual(anchor_slope_breaks(_s((0, -45.0), (20, -45.1), (40, -44.9))), [])

    def test_rate_error_is_clean(self):
        # 4.17% PAL over 20s is 0.83s of drift per step -- a ramp, not a boundary.
        self.assertEqual(anchor_slope_breaks(_s((0, 0.0), (20, 0.83), (40, 1.66))), [])

    def test_unresolved_block_boundary_is_reported(self):
        breaks = anchor_slope_breaks(_s((0, 0.1), (20, 0.2), (40, -12.0), (60, -12.1)))
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["at"], 40)
        self.assertLess(breaks[0]["step"], -5.0)

    def test_small_step_over_a_short_gap_is_not_a_boundary(self):
        # The subtitle's own local timing errors: steep slope, tiny step. This is
        # what the absolute bar exists for -- healthy files top out near 2-3s.
        self.assertEqual(anchor_slope_breaks(_s((0, 0.0), (2, 1.5), (22, 1.4))), [])

    def test_large_step_spread_over_minutes_is_a_ramp_not_a_step(self):
        # 12s over 600s is drift at 2%, already the stretch branch's business.
        self.assertEqual(anchor_slope_breaks(_s((0, 0.0), (600, 12.0))), [])

    def test_too_few_anchors_is_not_a_verdict(self):
        self.assertEqual(anchor_slope_breaks(_s((0, 0.1))), [])
        self.assertEqual(anchor_slope_breaks([]), [])

    def test_unanchored_samples_are_skipped_not_counted(self):
        self.assertEqual(anchor_slope_breaks(_s((0, 0.1), (20, None), (40, 0.2))), [])


if __name__ == "__main__":
    unittest.main()
