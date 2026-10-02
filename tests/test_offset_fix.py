"""One steady offset read from the dense pool (Brooklyn Nine-Nine S02E06: +2.0s, alass missed it)."""
from __future__ import annotations

import random
import statistics
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pysubs2
from verifyarr import pipeline as P

CFG = types.SimpleNamespace(min_change_seconds=0.25)
DURATION = 1200.0


def _pool(offset, n=200, span=1100.0, tilt=0.0, noise=0.2, seed=3):
    """(audio, cue) pairs: the cue sits `offset` before the audio, walking by `tilt`."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        a = 30 + span * i / n
        out.append((a, a - offset - tilt * (a - 30) / span + rng.uniform(-noise, noise)))
    return out


def _run(pts, duration=DURATION):
    """Run the offset fixer on a fixed pool; the pool read after the fix is the pool shifted back."""
    subs = pysubs2.SSAFile()
    subs.append(pysubs2.SSAEvent(start=1000, end=2000, text="x"))
    shift = statistics.median(a - c for a, c in pts)
    segs = [{"start": 0.0, "end": duration, "text": "x"}]

    def dense(conn, video, s, cfg):
        return [(a, c + shift) for a, c in pts]

    with mock.patch.object(P, "_dense_pool", side_effect=dense), \
            mock.patch.object(P, "_cached_full_segments", return_value=segs), \
            mock.patch.object(P, "_write_fix"):
        p = P._dense_probe(pts)
        return P._try_offset_from_dense(None, Path("v.mkv"), Path("s.srt"), CFG, Path("/"),
                                        subs, subs, False, pts, p)


class OffsetFixTests(unittest.TestCase):
    def test_steady_offset_is_fixed(self):
        out = _run(_pool(2.0))
        self.assertIsNotNone(out)
        self.assertEqual(out[2]["kind"], "offset")
        self.assertAlmostEqual(out[2]["worst"], 2.0, delta=0.2)

    def test_healthy_file_is_left_alone(self):
        self.assertIsNone(_run(_pool(0.3)))

    def test_pool_covering_only_the_start_is_not_trusted(self):
        # A PAL stretch reads as one flat offset while the original still matches early on.
        self.assertIsNone(_run(_pool(3.4, span=300.0)))

    def test_walking_pool_is_not_an_offset(self):
        self.assertIsNone(_run(_pool(2.0, tilt=5.0)))


if __name__ == "__main__":
    unittest.main()
