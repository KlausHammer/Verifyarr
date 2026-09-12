"""Timing math for generated subtitles -- the silence map, the chunk plan, and the mapping that
converts a Whisper timestamp measured against TRIMMED audio back to the video's own timeline.

Why this file exists, of everything in generate.py: these three are pure functions with no
network and no ffmpeg, and they are the ones whose failure mode is silent. A wrong threshold
somewhere else produces a visibly bad subtitle; a wrong offset here produces a subtitle that
looks completely normal and is simply shifted by the length of a silence nobody will think to
measure. Both bugs this file pins down were exactly that shape.

Run: python3 -m unittest discover -s tests
"""

import unittest

from verifyarr import generate as g


class SpeechRegions(unittest.TestCase):
    def test_entirely_silent_window_yields_nothing(self):
        # REGRESSION: the pad was applied at the window's own edges too, so a fully silent
        # window came back as two 0.2s slivers of pure silence instead of an empty list -- which
        # then got concatenated and uploaded. Whisper's most reliable response to a request
        # containing no speech at all is to invent a caption for it.
        self.assertEqual(g._speech_regions_from_silences(100.0, [(0.0, 100.0)]), [])

    def test_pad_protects_speech_but_not_the_window_edges(self):
        regions = g._speech_regions_from_silences(100.0, [(0.0, 5.0), (40.0, 50.0), (96.0, 100.0)])
        self.assertEqual(regions, [(4.8, 40.2), (49.8, 96.2)])

    def test_touching_silences_do_not_manufacture_a_region_between_them(self):
        # REGRESSION: two adjacent silences were padded independently, leaving a fake 0.4s
        # "speech" region in the middle of what is really one continuous silence.
        self.assertEqual(g._speech_regions_from_silences(100.0, [(10.0, 20.0), (20.0, 30.0)]),
                         [(0.0, 10.2), (29.8, 100.0)])

    def test_sub_threshold_regions_are_dropped(self):
        regions = g._speech_regions_from_silences(100.0, [(0.0, 50.0), (50.1, 100.0)])
        for start, end in regions:
            self.assertGreaterEqual(end - start, g.MIN_SPEECH_REGION_SECONDS)

    def test_range_helper_returns_absolute_times(self):
        self.assertEqual(g._speech_regions_in_range([(140.0, 150.0)], 100.0, 200.0),
                         [(100.0, 140.2), (149.8, 200.0)])

    def test_empty_range_is_not_an_error(self):
        self.assertEqual(g._speech_regions_in_range([], 50.0, 50.0), [])


class RemapThroughTrim(unittest.TestCase):
    MAPPING = [(0.0, 10.0, 0.0), (10.0, 20.0, 50.0)]

    def test_empty_mapping_passes_through(self):
        # REGRESSION: min() over an empty mapping raised ValueError. Unreachable from today's
        # callers, but it is the fallback branch of a function whose whole job is to never drop
        # a segment -- crashing is the one thing it must not do.
        self.assertEqual(g._remap_through_trim(1.0, 2.0, []), (1.0, 2.0))

    def test_segment_starting_exactly_on_a_piece_boundary_takes_the_later_piece(self):
        self.assertEqual(g._remap_through_trim(10.0, 12.0, self.MAPPING), (50.0, 52.0))

    def test_segment_straddling_a_boundary_follows_its_own_start(self):
        # Located by START, so a segment running across a cut keeps one consistent offset
        # rather than being torn in half.
        self.assertEqual(g._remap_through_trim(8.0, 12.0, self.MAPPING), (8.0, 12.0))

    def test_segment_past_the_end_falls_back_to_the_closest_piece(self):
        self.assertEqual(g._remap_through_trim(20.0, 21.0, self.MAPPING), (60.0, 61.0))

    def test_a_later_chunk_maps_to_absolute_video_time(self):
        # The mapping's third element is ABSOLUTE, so no caller has to add a chunk offset
        # afterwards -- that second addition is where an off-by-one-chunk error would live.
        self.assertEqual(g._remap_through_trim(0.5, 1.0, [(0.0, 30.0, 1800.0)]), (1800.5, 1801.0))


class PlanChunks(unittest.TestCase):
    def test_boundary_snaps_back_into_a_silence(self):
        # A silence at 560-570 (midpoint 565) sits within the snap window before the 600s
        # target, so the chunk ends there instead of cutting a word in half at 600.
        chunks = g.plan_chunks(3600.0, 600.0, [(560.0, 570.0)])
        self.assertAlmostEqual(chunks[0][1], 565.0)

    def test_snapping_never_makes_a_chunk_longer_than_configured(self):
        # Only ever pulled EARLIER. The configured length is a hard per-request limit for at
        # least one provider (Cloudflare), so overshooting it to reach a silence would trade a
        # cosmetic problem for a failed request.
        silences = [(x, x + 4.0) for x in range(50, 3600, 97)]
        for chunk_seconds in (60.0, 120.0, 600.0):
            for duration in (30.0, 61.0, 605.0, 1300.0, 3600.0, 7201.0):
                for start, end in g.plan_chunks(duration, chunk_seconds, silences):
                    self.assertLessEqual(end - start, chunk_seconds + 1e-6)

    def test_chunks_tile_the_whole_track_without_gaps_or_overlap(self):
        for duration in (30.0, 61.0, 605.0, 1300.0, 3600.0, 7201.0):
            chunks = g.plan_chunks(duration, 600.0, [(1000.0, 1010.0)])
            self.assertAlmostEqual(chunks[0][0], 0.0)
            self.assertAlmostEqual(chunks[-1][1], duration, places=5)
            for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
                self.assertAlmostEqual(prev_end, next_start, places=9)

    def test_no_sliver_final_chunk(self):
        # 605s of audio at a 600s target would leave a 5s tail; the remainder is split down the
        # middle instead. A 5s request is a wasted round trip and its own rate-limit slot.
        chunks = g.plan_chunks(605.0, 600.0, [])
        self.assertEqual(len(chunks), 2)
        for start, end in chunks:
            self.assertGreater(end - start, g.MIN_TAIL_CHUNK_SECONDS)

    def test_zero_and_short_durations(self):
        self.assertEqual(g.plan_chunks(0.0, 600.0, []), [])
        self.assertEqual(g.plan_chunks(10.0, 600.0, []), [(0.0, 10.0)])


class NormalizeLang(unittest.TestCase):
    def test_whisper_language_names_become_codes(self):
        # Whisper's verbose_json reports "english", not "en". Comparing that against the wanted
        # language raw is how an English subtitle gets "translated" from English to English --
        # a full LLM pass over the whole file, paid for, that can only make it worse.
        self.assertEqual(g.normalize_lang("English"), "en")
        self.assertEqual(g.normalize_lang("danish"), "da")

    def test_ffprobe_three_letter_tags_become_codes(self):
        self.assertEqual(g.normalize_lang("dan"), "da")
        self.assertEqual(g.normalize_lang("eng"), "en")

    def test_already_a_code_is_kept_and_lowercased(self):
        self.assertEqual(g.normalize_lang("DA"), "da")

    def test_unusable_values_are_none(self):
        for value in (None, "", "   ", "not a language"):
            self.assertIsNone(g.normalize_lang(value))


if __name__ == "__main__":
    unittest.main()
