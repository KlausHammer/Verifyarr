"""A rate in alass' output is only rescued when the original shows it too."""
import types
import unittest
from pathlib import Path
from unittest import mock

from verifyarr import pipeline


def _pool(f):
    return [(a, f(a)) for a in range(30, 2600, 9)]


def _subs(n=0):
    """A stand-in subtitle object: only the cue starts matter to the guard."""
    return types.SimpleNamespace(events=[types.SimpleNamespace(start=1000 * i + n) for i in range(5)])


class BaselineRampTests(unittest.TestCase):
    def check(self, pool, *baselines):
        with mock.patch.object(pipeline, "_dense_pool", return_value=pool):
            return pipeline._baseline_shows_ramp(None, Path("v.mkv"), None, *baselines)

    def test_flat_constant_offset_is_not_a_ramp(self):
        self.assertFalse(self.check(_pool(lambda a: a + 3.9), _subs()))

    def test_a_real_rate_is_a_ramp(self):
        self.assertTrue(self.check(_pool(lambda a: a * 1.0283 + 1.3), _subs()))

    def test_unknown_pool_keeps_the_old_behaviour(self):
        self.assertTrue(self.check([], _subs()))

    def test_an_empty_baseline_does_not_hide_a_flat_one(self):
        pools = [[], _pool(lambda a: a + 3.9)]
        with mock.patch.object(pipeline, "_dense_pool", side_effect=pools):
            self.assertFalse(pipeline._baseline_shows_ramp(None, Path("v.mkv"), None, _subs(1), _subs(2)))

    def test_a_pool_too_thin_to_probe_is_unknown_not_flat(self):
        self.assertTrue(self.check(_pool(lambda a: a + 3.9)[:3], _subs()))

    def test_a_block_shaped_pool_is_not_called_flat(self):
        # Steps of +-9 s: no tilt and no single line either, so the baseline proves nothing.
        self.assertTrue(self.check(_pool(lambda a: a + (9.0 if (a // 300) % 2 else -9.0)), _subs()))

    def test_identical_baselines_are_measured_once(self):
        with mock.patch.object(pipeline, "_dense_pool", return_value=_pool(lambda a: a + 3.9)) as dp:
            self.assertFalse(pipeline._baseline_shows_ramp(None, Path("v.mkv"), None, _subs(), _subs()))
        self.assertEqual(dp.call_count, 1)

    def check_rate(self, pool, slope):
        with mock.patch.object(pipeline, "_dense_pool", return_value=pool):
            return pipeline._baseline_shows_ramp(None, Path("v.mkv"), None, _subs(), ramp_slope=slope)

    def test_a_ramp_in_the_original_must_be_the_rescued_rate(self):
        # cue = 1.0283 a + 1.3 reads as slope -2.83 % in (audio, audio - cue).
        pool = _pool(lambda a: a * 1.0283 + 1.3)
        self.assertTrue(self.check_rate(pool, -0.0283))
        self.assertFalse(self.check_rate(pool, +0.039))

    def test_a_mild_drift_does_not_license_alass_own_staircase(self):
        # Avatar S01E14: the original drifts ~0.2 %, alass' output shows -3.9 %.
        pool = _pool(lambda a: a * 0.998 + 2.0)
        self.assertFalse(self.check_rate(pool, -0.039))

    def test_no_baseline_at_all_keeps_the_old_behaviour(self):
        self.assertTrue(self.check(_pool(lambda a: a), None))


if __name__ == "__main__":
    unittest.main()
