"""Fund 3: taersklen er brugerens ene regel, kodet to steder -- de skal foelges ad.

pipeline.SCREEN_TOLERANCE_S (screen: offset/spredning/drift) og
sync.min_change_seconds-defaulten (skriv: intet under taersklen skrives) er samme
beslutning. Testen her pinder vaerdien OG at de to steder er enige, saa en fremtidig
ensidig aendring fejler hoejlydt i stedet for at splitte adfaerden. Seloev maales
gennem matricen (arm 1/2, clean-cellen, genuine.py) -- se CACHE_RAPPORT.md.
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
                         "taersklen kodet to steder -- de er drevet fra hinanden")
        self.assertEqual(pipeline.SCREEN_TOLERANCE_S, DECIDED_SECONDS,
                         "brugerens beslutning er 0,25 s")


if __name__ == "__main__":
    unittest.main()
