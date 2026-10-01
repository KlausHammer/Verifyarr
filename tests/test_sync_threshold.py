"""Finding 3: the threshold is the user's one rule, coded in two places -- they must move together.

pipeline.SCREEN_TOLERANCE_S (screen: offset/spread/drift) and the
sync.min_change_seconds default (write: nothing under the threshold is written) are the same
decision. This test pins the value AND that the two places agree, so a future
one-sided change fails loudly instead of splitting the behaviour. The effect itself is measured
through the matrix (arm 1/2, the clean cell, genuine.py) -- see CACHE_RAPPORT.md.
"""
from __future__ import annotations

import unittest

from verifyarr import pipeline
from verifyarr.settings import SETTING_DEFS

DECIDED_SECONDS = 0.25


class ThresholdBoundaryTests(unittest.TestCase):
    def test_screen_and_write_gate_agree(self):
        default = SETTING_DEFS["sync.min_change_seconds"][2]
        self.assertEqual(default, pipeline.SCREEN_TOLERANCE_S,
                         "threshold coded in two places -- they have drifted apart")
        self.assertEqual(pipeline.SCREEN_TOLERANCE_S, DECIDED_SECONDS,
                         "the user's decision is 0.25 s")


if __name__ == "__main__":
    unittest.main()
