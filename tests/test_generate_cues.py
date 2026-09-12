"""Turning Whisper segments into subtitle-shaped cues, and the final SRT assembly.

The invariants here are the ones a reader of the finished file would notice immediately but a
developer changing the splitting heuristic would not: cues must cover exactly the span Whisper
reported, stay in order, never overlap, and never exceed the readable limits. Most of these are
easier to break than to notice, because a subtitle with one 12-second unreadable block still
plays fine.

Run: python3 -m unittest discover -s tests
"""

import unittest

from verifyarr import generate as g
from verifyarr.subtitles import build_srt_from_segments, MIN_CUE_DURATION_MS


class WrapIntoNParts(unittest.TestCase):
    def test_never_returns_more_parts_than_words(self):
        self.assertEqual(g._wrap_into_n_parts("a b", 5), ["a", "b"])

    def test_single_word_is_never_split(self):
        self.assertEqual(g._wrap_into_n_parts("indivisible", 4), ["indivisible"])

    def test_words_are_distributed_evenly_and_none_are_lost(self):
        parts = g._wrap_into_n_parts(" ".join(f"w{i}" for i in range(10)), 3)
        self.assertEqual(" ".join(parts).split(), [f"w{i}" for i in range(10)])
        self.assertEqual([len(p.split()) for p in parts], [4, 3, 3])


class SplitSegmentsIntoCues(unittest.TestCase):
    def test_ordinary_segment_passes_through_untouched(self):
        seg = {"start": 1.0, "end": 3.0, "text": "A perfectly ordinary line."}
        self.assertEqual(g.split_segments_into_cues([seg]), [seg])

    def test_empty_text_is_dropped(self):
        self.assertEqual(g.split_segments_into_cues([{"start": 1.0, "end": 2.0, "text": "   "}]), [])

    def test_oversized_sentence_is_wrapped_further(self):
        # REGRESSION: splitting on punctuation alone left "Hi." next to a 150-character
        # sentence, because only the segment as a whole was measured against max_chars.
        long_sentence = " ".join(["word"] * 30)
        cues = g.split_segments_into_cues([{"start": 0.0, "end": 5.0, "text": f"Hi. {long_sentence}"}])
        for cue in cues:
            self.assertTrue(len(cue["text"]) <= g.CUE_MAX_CHARS or len(cue["text"].split()) == 1)

    def test_parts_are_re_split_until_none_sits_on_screen_too_long(self):
        # REGRESSION: parts were sized by WORD count but given time by CHARACTER count, so an
        # even-looking split could still hand one piece 74% of the span and leave it over the
        # limit. One pass was not enough.
        cues = g.split_segments_into_cues(
            [{"start": 0.0, "end": 20.0, "text": "First sentence here. Second sentence here."}])
        for cue in cues:
            self.assertTrue(cue["end"] - cue["start"] <= g.CUE_MAX_SECONDS + 0.01
                            or len(cue["text"].split()) == 1)

    def test_unsplittable_long_word_terminates(self):
        # Nothing to split on; the function must give up rather than recurse forever.
        seg = {"start": 0.0, "end": 30.0, "text": "A" * 200}
        self.assertEqual(g.split_segments_into_cues([seg]), [seg])

    def test_end_before_start_never_produces_a_negative_cue(self):
        cues = g.split_segments_into_cues([{"start": 10.0, "end": 9.0, "text": "Hello there friend"}])
        for cue in cues:
            self.assertGreaterEqual(cue["end"], cue["start"])

    def test_zero_length_segment_with_several_sentences(self):
        cues = g.split_segments_into_cues([{"start": 10.0, "end": 10.0, "text": "One. Two. Three."}])
        self.assertEqual(len(cues), 3)
        for cue in cues:
            self.assertGreaterEqual(cue["end"], cue["start"])

    def test_split_covers_the_original_span_and_stays_in_order(self):
        # The property that actually matters: a split may redistribute time inside the segment,
        # but it may never move the segment's own boundaries or reorder what is said.
        import random
        rng = random.Random(1234)
        for _ in range(200):
            start = round(rng.uniform(0, 5000), 2)
            span = round(rng.uniform(0, 40), 2)
            words = [f"ord{i}." if rng.random() < 0.2 else f"ord{i}" for i in range(rng.randint(1, 60))]
            seg = {"start": start, "end": start + span, "text": " ".join(words)}
            cues = g.split_segments_into_cues([seg])
            self.assertTrue(cues)
            self.assertAlmostEqual(cues[0]["start"], start, places=6)
            self.assertGreaterEqual(cues[-1]["end"], start + span - 1e-6)
            self.assertEqual(" ".join(c["text"] for c in cues).split(), seg["text"].split())
            previous_end = None
            for cue in cues:
                self.assertGreaterEqual(cue["end"], cue["start"])
                if previous_end is not None:
                    self.assertGreaterEqual(cue["start"], previous_end - 1e-6)
                previous_end = cue["end"]


class BuildSrt(unittest.TestCase):
    def test_blank_cues_are_dropped_and_short_ones_floored(self):
        subs = build_srt_from_segments([{"start": 1.0, "end": 1.01, "text": "blink"},
                                        {"start": 5.0, "end": 6.0, "text": "  "}])
        self.assertEqual(len(subs.events), 1)
        self.assertEqual(subs.events[0].end - subs.events[0].start, MIN_CUE_DURATION_MS)

    def test_events_are_written_in_chronological_order(self):
        subs = build_srt_from_segments([{"start": 5.0, "end": 6.0, "text": "second"},
                                        {"start": 1.0, "end": 2.0, "text": "first"}])
        self.assertEqual([e.plaintext for e in subs.events], ["first", "second"])

    def test_the_minimum_duration_floor_cannot_create_an_overlap(self):
        # The floor stretches a 10ms cue to 500ms -- straight over the next cue 200ms later.
        # Two cues on screen at once renders as a doubled caption in most players, which is a
        # real defect, unlike the too-short cue the floor was there to avoid.
        subs = build_srt_from_segments([{"start": 10.0, "end": 10.01, "text": "blink"},
                                        {"start": 10.2, "end": 11.0, "text": "next"}])
        self.assertEqual(subs.events[0].end, subs.events[1].start)

    def test_simultaneous_cues_sharing_a_start_are_left_alone(self):
        subs = build_srt_from_segments([{"start": 3.0, "end": 5.0, "text": "speaker one"},
                                        {"start": 3.0, "end": 5.0, "text": "speaker two"}])
        self.assertEqual(len(subs.events), 2)
        for event in subs.events:
            self.assertGreater(event.end, event.start)

    def test_no_overlaps_survive_a_realistic_pipeline(self):
        segments = g.split_segments_into_cues(
            [{"start": i * 2.0, "end": i * 2.0 + 1.99, "text": f"Line {i}. Another clause here."}
             for i in range(40)])
        subs = build_srt_from_segments(segments)
        for current, following in zip(subs.events, subs.events[1:]):
            self.assertLessEqual(current.end, following.start)


if __name__ == "__main__":
    unittest.main()
