"""When a sampled run buys the full transcript.

A trigger only decides whether to LOOK properly; the full pass re-checks every gate
on its own. So each trigger's bar sits below the bar of what it triggers -- the
measured misses below all came from a trigger that borrowed the fix's own gates.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from verifyarr import pipeline as P


def _cfg(**over):
    base = dict(whisper_mode="sampled", escalate_sampled_to_full=True,
                escalate_only_multi_block=True, escalate_min_bad_samples=2,
                fps_check_enabled=True, fps_require_full_coverage=False)
    base.update(over)
    return SimpleNamespace(**base)


def _samples(*shifts):
    return {"samples": [{"start": 60.0 * i, "anchor": None if sh is None else {"shift": sh},
                         "score": 0.8} for i, sh in enumerate(shifts)]}


class ScreenTriggerTests(unittest.TestCase):
    def test_multi_block_fit_escalates_even_when_anchors_agree(self):
        # A half-repaired block file: the displaced text never anchors, so the anchors
        # that exist agree. alass' own block fit is the suspicion.
        self.assertTrue(P._screen_says_needs_full(_samples(0.3, -0.2, 0.4, None, None), _cfg(), 3))

    def test_single_block_with_agreeing_anchors_stays_sampled(self):
        self.assertFalse(P._screen_says_needs_full(_samples(0.3, -0.2, 0.4, 0.1), _cfg(), 1))

    def test_lone_huge_anchor_escalates_without_a_block_fit(self):
        # C_S03E09 piecewise_c: alass saw one offset, one anchor sat 20s out.
        self.assertTrue(P._screen_says_needs_full(_samples(19.96, 0.57, 0.52), _cfg(), 1))

    def test_anchor_under_ten_seconds_alone_does_not(self):
        self.assertFalse(P._screen_says_needs_full(_samples(8.8, 0.2, 0.1), _cfg(), 1))

    def test_switch_off_or_full_mode_never_escalates(self):
        s = _samples(19.96, 0.57)
        self.assertFalse(P._screen_says_needs_full(s, _cfg(escalate_sampled_to_full=False), 3))
        self.assertFalse(P._screen_says_needs_full(s, _cfg(whisper_mode="full"), 3))


def _drift_pool(total_s=-1.4, n=62):
    # (audio, subtitle) pairs of a 24->23.976 file: the cue walks total_s over the episode.
    return {"fps_points": [(a, a - total_s * a / 1300.0) for a in
                           (20.0 + 1280.0 * i / (n - 1) for i in range(n))]}


# C_S02E04 sampled as measured: plain tilt passes, binned misses 0.90 by 0.02s. The English
# file drifts 0.1% (VAD tilt -1.06s); the Danish one the user watched does not (-0.14s).
C_S02E04_SIG = {"tilt": -1.393, "binned": -0.879, "n": 62, "rho": -0.517,
                "drops": [-1.3, -1.4, -1.35, -1.2, -1.45, -1.3, -1.38, -1.25]}


class FpsTriggerTests(unittest.TestCase):
    def test_tilt_alone_buys_the_look_when_the_fix_gates_would_veto(self):
        with mock.patch.object(P, "anchor_drift_signature", return_value=C_S02E04_SIG):
            self.assertIsNone(P._anchor_signature_passes(C_S02E04_SIG))  # the fix would refuse
            self.assertTrue(P._fps_says_needs_full(_drift_pool(), _cfg()))

    def test_flat_pool_does_not_escalate(self):
        self.assertFalse(P._fps_says_needs_full(_drift_pool(total_s=-0.2), _cfg()))

    def test_too_few_anchors_does_not_escalate(self):
        self.assertFalse(P._fps_says_needs_full(_drift_pool(n=10), _cfg()))

    def test_already_full_coverage_does_not_escalate(self):
        pool = dict(_drift_pool(), full_coverage=True)
        self.assertFalse(P._fps_says_needs_full(pool, _cfg()))

    def test_strict_mode_keeps_the_fix_gates(self):
        with mock.patch.object(P, "anchor_drift_signature", return_value=C_S02E04_SIG):
            self.assertFalse(P._fps_says_needs_full(_drift_pool(), _cfg(fps_require_full_coverage=True)))


if __name__ == "__main__":
    unittest.main()
