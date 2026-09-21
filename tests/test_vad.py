"""Focused tests for verifyarr/vad.py -- speech-timeline traffic control.

All language-independent by construction: no word lists, no language data, only
second-arithmetic on intervals. Uses a stub subtitle object (no fixtures needed)."""
import unittest

from verifyarr import vad


class _Ev:
    def __init__(self, start_ms, end_ms):
        self.start = start_ms
        self.end = end_ms


class _Subs:
    def __init__(self, cues):
        self.events = [_Ev(s * 1000, e * 1000) for s, e in cues]


# One cue every 10s across the regions used below -- every candidate window overlaps one.
SUBS = _Subs([(t, t + 2) for t in range(0, 1300, 10)])


class ParseTests(unittest.TestCase):
    def test_tsv_with_header(self):
        ivs = vad.parse_vad_tsv("start_s\tend_s\n14.15\t15.04\n16.48\t17.28\n")
        self.assertEqual(ivs, [(14.15, 15.04), (16.48, 17.28)])

    def test_tsv_skips_garbage(self):
        self.assertEqual(vad.parse_vad_tsv("nope\n1.0\n2.0\t1.0\n-3\t-1\n"), [])

    def test_segments_passthrough(self):
        segs = [{"start": 1.0, "end": 2.0}, {"nope": 1}, {"start": 5.0, "end": 4.0}]
        self.assertEqual(vad.segments_to_intervals(segs), [(1.0, 2.0)])


class CoverageTests(unittest.TestCase):
    def test_partial_overlap(self):
        self.assertAlmostEqual(vad.speech_coverage([(0.0, 10.0)], 5.0, 15.0), 5.0)

    def test_sums_intervals(self):
        self.assertAlmostEqual(vad.speech_coverage([(0.0, 5.0), (10.0, 12.0)], 0.0, 30.0), 7.0)

    def test_no_overlap(self):
        self.assertEqual(vad.speech_coverage([(0.0, 5.0)], 10.0, 20.0), 0.0)


class PickTests(unittest.TestCase):
    def test_no_timeline_returns_base_untouched(self):
        self.assertEqual(vad.pick_sample_time(SUBS, None, 100.0, 200.0, 30.0, 150.0), 150.0)

    def test_tie_returns_base(self):
        ivs = [(0.0, 1300.0)]  # speech everywhere: every candidate ties
        self.assertEqual(vad.pick_sample_time(SUBS, ivs, 100.0, 200.0, 30.0, 150.0), 150.0)

    def test_nudges_onto_speech(self):
        # Speech only at 115-140: base 100.0 must move to the covering candidate.
        ivs = [(115.0, 140.0)]
        got = vad.pick_sample_time(SUBS, ivs, 90.0, 220.0, 30.0, 100.0)
        self.assertIsNotNone(got)
        self.assertLessEqual(got, 115.0)
        self.assertGreaterEqual(got + 30.0, 140.0)

    def test_silence_returns_none(self):
        self.assertIsNone(vad.pick_sample_time(SUBS, [], 100.0, 200.0, 30.0, 150.0))

    def test_deterministic(self):
        ivs = [(140.0, 200.0), (300.0, 310.0)]
        args = (SUBS, ivs, 100.0, 400.0, 30.0, 150.0)
        self.assertEqual(vad.pick_sample_time(*args), vad.pick_sample_time(*args))

    def test_scan_finds_densest(self):
        ivs = [(300.0, 330.0)]
        got = vad.scan_region_time(SUBS, ivs, 100.0, 400.0, 30.0)
        self.assertIsNotNone(got)
        self.assertLessEqual(got, 300.0)
        self.assertGreaterEqual(got + 30.0, 330.0)

    def test_scan_silent_region_none(self):
        self.assertIsNone(vad.scan_region_time(SUBS, [(0.0, 1.0)], 100.0, 400.0, 30.0))


class RunnerTests(unittest.TestCase):
    def test_unconfigured_returns_none(self):
        class Cfg:
            pass
        intervals, whole = vad.timeline_for_video(None, "/nonexistent.mkv", Cfg())
        self.assertIsNone(intervals)
        self.assertTrue(whole, "no timeline at all is not a partial one")
        self.assertIsNone(vad.run_vad_timeline("/nonexistent.mkv", "", ""))
        self.assertIsNone(vad.run_vad_timeline("/nonexistent.mkv", "/no/bin", "/no/model"))




class PartialTimelineTests(unittest.TestCase):
    """Cached clips map only what has been sampled. Absence of data there is not silence."""

    def test_partial_timeline_never_reports_silence(self):
        # one clip's worth of speech at 100-120s; the region at 600s was never sampled
        ivs = [(100.0, 120.0)]
        self.assertIsNone(vad.pick_sample_time(SUBS, ivs, 600.0, 700.0, 30.0, 650.0),
                          "whole-file timeline should still call an empty stretch silent")
        self.assertEqual(vad.pick_sample_time(SUBS, ivs, 600.0, 700.0, 30.0, 650.0,
                                              whole_file=False),
                         650.0, "unsampled region was written off as silence")

    def test_partial_timeline_still_nudges_onto_known_speech(self):
        """Moving onto speech we KNOW about is safe on partial data -- only the silence
        verdict is not. Losing the nudge would be the wrong way to fix this."""
        ivs = [(150.0, 200.0)]
        got = vad.pick_sample_time(SUBS, ivs, 90.0, 220.0, 30.0, 160.0, whole_file=False)
        self.assertIsNotNone(got)
        self.assertGreater(vad.speech_coverage(ivs, got, got + 30.0), 0.0)


if __name__ == "__main__":
    unittest.main()
