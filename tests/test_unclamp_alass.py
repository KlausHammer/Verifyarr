"""alass clamps cues it shifts before 0 s; a stretch fix on top must not pile them up at one instant."""
import unittest

import pysubs2

from verifyarr import pipeline as P


def _subs(starts):
    s = pysubs2.SSAFile()
    for t in starts:
        s.events.append(pysubs2.SSAEvent(start=int(t * 1000), end=int(t * 1000) + 1500, text="x"))
    return s


class Unclamp(unittest.TestCase):
    def setUp(self):
        self.base = _subs([1.2, 3.6, 5.5, 7.6] + [30 + 10 * i for i in range(60)])
        # alass sent the file along a = 0.961 line, b = -38: the first cues land before 0 and are clamped.
        self.cur = _subs([max(0.0, 0.961 * e.start / 1000 - 38) for e in self.base.events])

    def test_clamped_cues_are_put_back_on_alass_line(self):
        out = P._unclamp_alass_output(self.cur, self.base)
        self.assertEqual(len({e.start for e in out.events[:4]}), 4)
        self.assertAlmostEqual(out.events[0].start / 1000, 0.961 * 1.2 - 38, delta=0.01)
        self.assertEqual(self.cur.events[0].start, 0)          # input untouched

    def test_nothing_clamped_returns_input(self):
        base = _subs([40 + 10 * i for i in range(30)])
        cur = _subs([e.start / 1000 - 5 for e in base.events])
        self.assertIs(P._unclamp_alass_output(cur, base), cur)

    def test_clamped_without_baseline_is_refused(self):
        self.assertIsNone(P._unclamp_alass_output(self.cur, None))

    def test_not_one_line_is_refused(self):
        cur = _subs([max(0.0, e.start / 1000 - 38 + (i % 2) * 3000) for i, e in enumerate(self.base.events)])
        self.assertIsNone(P._unclamp_alass_output(cur, self.base))


if __name__ == "__main__":
    unittest.main()
