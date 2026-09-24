"""Per-cue noise hides from the anchors' shifts but not from their own spread.

Random +/-1..3s per cue has no offset to find: each clip median averages it away, so
the shifts look healthy. The disagreement between one clip's own lines survives.
"""
from __future__ import annotations

import unittest

from verifyarr.correctness import anchor_jitter, JITTER_MIN_MAD_S


def _s(*mads, shift=0.2):
    return [{"start": 60.0 * i, "anchor": None if m is None else {"shift": shift, "mad": m}}
            for i, m in enumerate(mads)]


class AnchorJitterTests(unittest.TestCase):
    def test_healthy_file_stays_under_the_bar(self):
        # Healthy peak, measured: SH_S01E01 uniform_neg sampled, 0.445s.
        self.assertLess(anchor_jitter(_s(0.3, 0.445, 0.5, 0.44, 0.45)), JITTER_MIN_MAD_S)
        self.assertGreater(anchor_jitter(_s(0.3, 0.445, 0.5, 0.44, 0.45)), 0.44)

    def test_jittered_file_crosses_it(self):
        # C_S02E03 jitter: 0.56s median, silent before.
        self.assertGreaterEqual(anchor_jitter(_s(0.5, 0.6, 0.55, 0.7, 0.45, 0.56)), JITTER_MIN_MAD_S)

    def test_missing_anchors_do_not_count(self):
        self.assertAlmostEqual(anchor_jitter(_s(0.6, None, 0.6, None, 0.6, 0.6, 0.6)), 0.6)

    def test_too_few_anchors_is_not_a_verdict(self):
        self.assertIsNone(anchor_jitter(_s(0.9, 0.9, 0.9, 0.9)))

    def test_anchor_without_mad_is_skipped(self):
        samples = _s(0.6, 0.6, 0.6, 0.6, 0.6)
        samples.append({"start": 999.0, "anchor": {"shift": 0.1}})
        self.assertAlmostEqual(anchor_jitter(samples), 0.6)


if __name__ == "__main__":
    unittest.main()
