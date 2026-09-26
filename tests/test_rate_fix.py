"""Whole-file rate from dense full-transcript anchors: gates, snapping, flatness."""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import subtitles as S


def _pool(rate, offset, n=200, span=3000.0, noise=0.2, seed=1):
    """(audio, cue) pairs for a cue track stretched by rate plus offset."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        a = 30 + span * i / n
        out.append((a + rng.uniform(-noise, noise), a * (1 + rate) + offset))
    return out


def _probe(pts):
    return S.stretch_probe([(a, a - s) for a, s in pts])


class RateGateTests(unittest.TestCase):
    def test_dead_zone_rate_passes(self):
        # 0.25% on 50 min walks 7.5s: under the old 8s stretch floor.
        self.assertTrue(S.rate_gates_pass(_probe(_pool(0.0025, -3.0))))

    def test_ntsc_rate_passes(self):
        self.assertTrue(S.rate_gates_pass(_probe(_pool(0.001, 0.0))))

    def test_healthy_file_fails(self):
        self.assertFalse(S.rate_gates_pass(_probe(_pool(0.0, 0.1))))

    def test_block_fails(self):
        pts = _pool(0.0, 0.0)
        pts = [(a, s + (4.0 if 1000 < a < 1600 else 0.0)) for a, s in pts]
        self.assertFalse(S.rate_gates_pass(_probe(pts)))

    def test_constant_offset_fails(self):
        self.assertFalse(S.rate_gates_pass(_probe(_pool(0.0, 7.0))))


class SnapTests(unittest.TestCase):
    def test_pal_snaps_exactly(self):
        pts = _pool(25 / 24 - 1, 2.0)
        p = _probe(pts)
        ratio, off, name = S.snap_rate(pts, p)
        self.assertAlmostEqual(ratio, 24 / 25, places=9)
        self.assertIn("25", name)

    def test_nearest_of_two_close_ratios_wins(self):
        # 25/24 and 25/23.976 sit 0.1 points apart; both inside the tolerance.
        pts = _pool((24000 / 1001) / 25 - 1, -7.9)
        p = dict(_probe(pts))
        p["slope"] = 1 - 1 / 1.04232  # measured between them (SH_S01E03)
        ratio, _, name = S.snap_rate(pts, p)
        self.assertAlmostEqual(ratio, 25 / (24000 / 1001), places=9)
        self.assertEqual(name, "23.976/25")

    def test_arbitrary_rate_keeps_measured(self):
        pts = _pool(0.0071, 2.0)
        p = _probe(pts)
        ratio, off, name = S.snap_rate(pts, p)
        self.assertAlmostEqual(ratio, 1 / 1.0071, places=4)
        self.assertNotIn("/", name)

    def test_fix_lands_on_audio(self):
        pts = _pool(-0.0022, -9.8, noise=0.0)
        ratio, off, _ = S.snap_rate(pts, _probe(pts))
        worst = max(abs(a - ratio * (s + off)) for a, s in pts)
        self.assertLess(worst, 0.05)


class FlatTests(unittest.TestCase):
    def test_healthy_is_flat(self):
        self.assertTrue(S.rate_is_flat(_probe(_pool(0.0, 0.1))))

    def test_leftover_ramp_is_not_flat(self):
        # alass' constant shift on a 0.3% drift: centred, still tilted.
        self.assertFalse(S.rate_is_flat(_probe(_pool(0.003, -4.5))))

    def test_staircase_is_not_tight_flat(self):
        # alass blocks on a ramp: flat on average, 1.8s steps.
        pts = [(a, s + 0.006 * (((a - 30) % 300) - 150)) for a, s in _pool(0.0, 0.0)]
        p = _probe(pts)
        self.assertLess(abs(p["tilt"]), S.RATE_TIGHT_TILT_S)
        self.assertFalse(S.rate_is_flat(p, tight=True))

    def test_offset_is_not_flat(self):
        self.assertFalse(S.rate_is_flat(_probe(_pool(0.0, 1.0))))


if __name__ == "__main__":
    unittest.main()
