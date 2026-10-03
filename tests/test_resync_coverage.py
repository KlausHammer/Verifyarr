"""_resync_verified must not accept a correction it could not re-measure everywhere.

A region still wrong after a partial repair produces NO anchor there, not a bad one
(the blind spot _confirmed_in_every_block exists for), so "every surviving anchor is
clean" passes on a half-right fix. Measured before this guard: 28 of 30 silent block
rows reported "fixed" at rec 0.42-0.90.
"""
from __future__ import annotations

import unittest

from verifyarr.pipeline import _regions_without_anchor


def _plan(*cuts):
    """A plan with len(cuts)+1 regions, cut at the given audio seconds."""
    return [{"cut_audio_s": c} for c in cuts] + [{"cut_audio_s": None}]


def _anchors(*starts):
    return [{"start": s} for s in starts]


class RegionCoverageTests(unittest.TestCase):
    def test_every_wide_region_anchored_is_verified(self):
        self.assertEqual(
            _regions_without_anchor(_plan(600.0), _anchors(100.0, 900.0), span=15.0), [])

    def test_unanchored_wide_region_is_reported(self):
        # Both anchors sit in region 0; region 1 (600s..inf) measured nothing.
        self.assertEqual(
            _regions_without_anchor(_plan(600.0), _anchors(100.0, 300.0), span=15.0), [1])

    def test_middle_region_gap_is_reported(self):
        self.assertEqual(
            _regions_without_anchor(_plan(300.0, 600.0), _anchors(100.0, 900.0), span=15.0), [1])

    def test_narrow_region_is_exempt(self):
        # 310-300 = 10s < 2*span: every anchor there straddles a cut by construction.
        self.assertEqual(
            _regions_without_anchor(_plan(300.0, 310.0), _anchors(100.0, 900.0), span=15.0), [])

    def test_no_anchors_at_all_reports_every_wide_region(self):
        self.assertEqual(
            _regions_without_anchor(_plan(600.0), _anchors(), span=15.0), [0, 1])

    def test_single_region_plan_needs_one_anchor(self):
        self.assertEqual(_regions_without_anchor(_plan(), _anchors(42.0), span=15.0), [])
        self.assertEqual(_regions_without_anchor(_plan(), _anchors(), span=15.0), [0])


if __name__ == "__main__":
    unittest.main()
