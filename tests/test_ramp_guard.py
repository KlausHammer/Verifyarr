"""A rate in alass' output is only rescued when the original shows it too."""
import unittest
from pathlib import Path
from unittest import mock

from verifyarr import pipeline


def _pool(f):
    return [(a, f(a)) for a in range(30, 2600, 9)]


class BaselineRampTests(unittest.TestCase):
    def check(self, pool, *baselines):
        with mock.patch.object(pipeline, "_dense_pool", return_value=pool):
            return pipeline._baseline_shows_ramp(None, Path("v.mkv"), None, *baselines)

    def test_flat_constant_offset_is_not_a_ramp(self):
        self.assertFalse(self.check(_pool(lambda a: a + 3.9), object()))

    def test_a_real_rate_is_a_ramp(self):
        self.assertTrue(self.check(_pool(lambda a: a * 1.0283 + 1.3), object()))

    def test_unknown_pool_keeps_the_old_behaviour(self):
        self.assertTrue(self.check([], object()))

    def test_no_baseline_at_all_keeps_the_old_behaviour(self):
        self.assertTrue(self.check(_pool(lambda a: a), None))


if __name__ == "__main__":
    unittest.main()
