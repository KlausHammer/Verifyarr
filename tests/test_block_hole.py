"""Huller, afkortning og blokke i realistiske størrelser (kun detektion).

Randomiserede scenarier (seedet) + detektorer: ♪-filter og lavere barre for
huller, hoved/hale-tjek for afkortning, klynge-detektor for blokke.
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

import pysubs2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verifyarr import correctness as C
import e2e_matrix as M


def _subs(*spans):
    """(start_s, end_s, text) -> SSAFile."""
    subs = pysubs2.SSAFile()
    for s0, s1, text in spans:
        subs.append(pysubs2.SSAEvent(start=int(s0 * 1000), end=int(s1 * 1000), text=text))
    return subs


def _segs(*spans):
    """(start_s, end_s, text) -> transcript segments."""
    return [{"start": s0, "end": s1, "text": t} for s0, s1, t in spans]


def _s(*pairs):
    return [{"start": t, "anchor": None if sh is None else {"shift": sh}} for t, sh in pairs]


class CueGapsTests(unittest.TestCase):
    def test_nested_cue_does_not_shrink_gap(self):
        # B nested in A; gap A->C must measure from A's end, not B's.
        subs = _subs((0, 100, "a"), (10, 20, "b"), (200, 210, "c"))
        self.assertEqual(C.cue_gaps(subs, 20.0), [(100.0, 200.0)])

    def test_overlapping_cues_give_no_gap(self):
        subs = _subs((0, 50, "a"), (40, 90, "b"), (80, 100, "c"))
        self.assertEqual(C.cue_gaps(subs, 20.0), [])


class MusicFilterTests(unittest.TestCase):
    def test_music_segments_do_not_count_as_speech(self):
        subs = _subs((0, 10, "a"), (200, 210, "b"))
        segs = _segs((20, 180, "Surrounded by losers ♪"),
                     (30, 170, "misfits and boozers ♪"))
        secs, words = C.gap_speech(segs, 10.0, 200.0)
        self.assertEqual((secs, words), (0.0, 0))

    def test_dialogue_next_to_music_still_counts(self):
        subs = _subs((0, 10, "a"), (200, 210, "b"))
        segs = _segs((20, 30, "hello there friend"),
                     (40, 180, "la la ♪"))
        secs, words = C.gap_speech(segs, 10.0, 200.0)
        self.assertEqual(words, 3)
        self.assertGreater(secs, 0)


class SongWindowTests(unittest.TestCase):
    def test_bare_lyrics_between_music_marks_are_ignored(self):
        raw = _segs((30, 34, "(upbeat music)"), (40, 50, "bare lyric one two three"),
                    (60, 70, "more bare lyric words here"), (80, 84, "♪ la la ♪"))
        kept = [s for s in raw if "(" not in s["text"]]
        self.assertGreater(C.gap_speech(kept, 10.0, 200.0)[1], 0)
        self.assertEqual(C.gap_speech(kept, 10.0, 200.0, C.music_spans(raw)), (0.0, 0))

    def test_speech_outside_the_song_still_counts(self):
        raw = _segs((30, 50, "(music)"), (120, 140, "a real scene goes on here now"))
        kept = [raw[1]]
        secs, words = C.gap_speech(kept, 10.0, 200.0, C.music_spans(raw))
        self.assertEqual(words, 7)

    def test_no_marks_changes_nothing(self):
        raw = _segs((40, 50, "just talking here"))
        self.assertEqual(C.gap_speech(raw, 10.0, 200.0, C.music_spans(raw)),
                         C.gap_speech(raw, 10.0, 200.0))


class MissingMiddleTests(unittest.TestCase):
    def test_lower_bar_catches_120_words(self):
        subs = _subs((0, 10, "a"), (500, 510, "b"))
        text = " ".join(f"w{i}" for i in range(130))
        segs = _segs((20, 400, text))
        hit = C.missing_middle_evidence(subs, segs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["words"], 130)

    def test_healthy_song_gap_stays_silent(self):
        subs = _subs((0, 10, "a"), (500, 510, "b"))
        segs = _segs((20, 400, " ".join(f"w{i} ♪" for i in range(130))))
        self.assertIsNone(C.missing_middle_evidence(subs, segs))

    def test_truncated_end_is_a_gap(self):
        subs = _subs((0, 10, "a"), (100, 110, "b"))
        text = " ".join(f"w{i}" for i in range(150))
        segs = _segs((120, 400, text))
        hit = C.missing_middle_evidence(subs, segs, duration_s=2000.0)
        self.assertIsNotNone(hit)
        self.assertGreaterEqual(hit["gap_start"], 110.0)

    def test_truncated_start_is_a_gap(self):
        subs = _subs((500, 510, "a"), (600, 610, "b"))
        text = " ".join(f"w{i}" for i in range(150))
        segs = _segs((20, 400, text))
        hit = C.missing_middle_evidence(subs, segs, duration_s=2000.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["gap_start"], 0.0)

    def test_short_credits_tail_stays_silent(self):
        subs = _subs((0, 10, "a"), (100, 110, "b"))
        segs = _segs((120, 160, "the end credits roll"))
        hit = C.missing_middle_evidence(subs, segs, duration_s=200.0)
        self.assertIsNone(hit)

    def test_tiny_bar_is_lower(self):
        self.assertEqual(C.missing_middle_min_words("ggml-tiny.en.bin"), 50)
        self.assertEqual(C.missing_middle_min_words("tiny.en-greedy-cpu"), 50)
        self.assertEqual(C.missing_middle_min_words("ggml-large-v3-turbo.bin"), 100)
        self.assertEqual(C.missing_middle_min_words(None), 100)

    def test_seventy_words_caught_on_tiny_only(self):
        # A 16-line skip on tiny carries ~71 words (SH_S01E03 tail).
        subs = _subs((0, 10, "a"), (500, 510, "b"))
        segs = _segs((20, 60, " ".join(f"w{i}" for i in range(71))))
        self.assertIsNotNone(C.missing_middle_evidence(subs, segs, min_words=50))
        self.assertIsNone(C.missing_middle_evidence(subs, segs))


class DeclaredMusicGapTests(unittest.TestCase):
    def _song_gap(self, cue_text, gap_s=43.0):
        subs = _subs((0, 10, "a"), (100, 102, cue_text), (102 + gap_s, 110 + gap_s, "b"))
        lyrics = " ".join(f"w{i}" for i in range(60))  # tiny writes lyrics without ♪
        segs = _segs((105, 100 + gap_s, lyrics))
        return C.missing_middle_evidence(subs, segs, min_words=50)

    def test_song_announced_by_the_subtitle_is_not_a_hole(self):
        # Breaking Bad S01E01 55:01: 43s song after this cue, 60 words on tiny.
        self.assertIsNone(self._song_gap('[MICK HARVEY\'S\\N"OUT OF TIME, MAN" PLAYS]'))
        self.assertIsNone(self._song_gap("[♪♪♪]"))

    def test_other_cues_still_open_a_hole(self):
        self.assertIsNotNone(self._song_gap("[VOMITING]"))
        self.assertIsNotNone(self._song_gap("♪ Give me the hope ♪"))  # lyrics are speech

    def test_long_gap_after_music_cue_is_still_judged(self):
        self.assertIsNotNone(self._song_gap("[MUSIC PLAYING]", gap_s=300.0))


class OverrunTests(unittest.TestCase):
    def test_speech_after_the_audio_ends_is_evidence(self):
        subs = _subs((0, 10, "a"), (1195, 1198, "late line"), (1210, 1212, "later"))
        self.assertEqual(C.overrun_evidence(subs, 1200.0), {"n": 1, "over_s": 12.0})

    def test_ending_inside_the_audio_or_non_speech_is_not(self):
        self.assertIsNone(C.overrun_evidence(_subs((0, 10, "a"), (1199, 1201, "b")), 1200.0))
        self.assertIsNone(C.overrun_evidence(_subs((0, 10, "a"), (1250, 1255, "[MUSIC]"),
                                                   (1260, 1262, "♪ la la ♪")), 1200.0))
        self.assertIsNone(C.overrun_evidence(_subs((0, 10, "a")), None))


class MatrixModelKeyTests(unittest.TestCase):
    def test_cfg_names_the_tested_model(self):
        # Transcript cache and hole bar key on the tested model, not the default.
        import tempfile
        from verifyarr import db
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            cfg = M.cfg_for(conn, groq_model="turbo-q5_0")
            conn.close()
        self.assertEqual(M.full_transcript_cache_key(cfg)[1], "ggml-turbo-q5_0.bin")


class BlockClusterTests(unittest.TestCase):
    def test_two_close_anchors_off_the_median_flag(self):
        pts = _s((0, 0.1), (600, 0.0), (1275, -3.8), (1329, -3.5), (2000, 0.1))
        clusters = C.anchor_block_clusters(pts)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_two_scattered_anchors_do_not_flag(self):
        pts = _s((0, 3.0), (600, 0.0), (1200, 0.1), (1800, -3.0), (2400, 0.0))
        self.assertEqual(C.anchor_block_clusters(pts), [])

    def test_single_huge_anchor_flags(self):
        pts = _s((0, 0.1), (600, 0.0), (1200, -7.0), (1800, 0.1))
        clusters = C.anchor_block_clusters(pts)
        self.assertEqual(len(clusters), 1)

    def test_healthy_jitter_stays_silent(self):
        pts = _s((0, 0.2), (600, -0.5), (1200, 0.8), (1800, -1.0), (2400, 0.4))
        self.assertEqual(C.anchor_block_clusters(pts), [])


class RandomScenarioTests(unittest.TestCase):
    def _base(self, dur_s=3000.0, step_s=5.0):
        subs = pysubs2.SSAFile()
        t = 0.0
        while t + 2.0 < dur_s:
            subs.append(pysubs2.SSAEvent(start=int(t * 1000), end=int((t + 2) * 1000),
                                         text=f"line {t:.0f}"))
            t += step_s
        return subs

    def test_hole_draw_is_seeded_and_in_range(self):
        subs = self._base()
        a, _, d1 = M.corrupt_hole_random(subs, random.Random("matrix-v1:SH_S01E01:hole_rand0"))
        b, _, d2 = M.corrupt_hole_random(subs, random.Random("matrix-v1:SH_S01E01:hole_rand0"))
        self.assertEqual(d1, d2)
        self.assertEqual([e.start for e in a.events], [e.start for e in b.events])
        self.assertGreaterEqual(d1["gap_end_s"] - d1["gap_start_s"], 60.0)
        self.assertLessEqual(d1["gap_end_s"] - d1["gap_start_s"], 300.0)

    def test_block_draw_is_seeded_and_in_range(self):
        subs = self._base()
        a, _, d1 = M.corrupt_block_random(subs, random.Random("matrix-v1:SH_S01E01:block_rand0"))
        b, _, d2 = M.corrupt_block_random(subs, random.Random("matrix-v1:SH_S01E01:block_rand0"))
        self.assertEqual(d1, d2)
        self.assertGreaterEqual(d1["length_s"], 90.0)
        self.assertLessEqual(d1["length_s"], 600.0)
        self.assertGreaterEqual(abs(d1["shift_s"]), 2.0)
        self.assertLessEqual(abs(d1["shift_s"]), 20.0)

    def test_trunc_draws_remove_head_or_tail_only(self):
        subs = self._base()
        out, kept, d = M.corrupt_trunc_start_random(
            subs, random.Random("matrix-v1:SH_S01E01:trunc_start_rand0"))
        self.assertTrue(all(i >= (len(subs.events) - len(kept)) for i in kept))
        self.assertGreaterEqual(d["removed_s"], 60.0)
        self.assertLessEqual(d["removed_s"], 300.0)
        out, kept, d = M.corrupt_trunc_end_random(
            subs, random.Random("matrix-v1:SH_S01E01:trunc_end_rand0"))
        self.assertEqual(kept, list(range(len(kept))))
        self.assertGreaterEqual(d["removed_s"], 60.0)

    def test_old_scenarios_unchanged(self):
        subs = self._base()
        _, _, d = M.corrupt_missing_middle(subs, random.Random("x"))
        self.assertEqual(d["gap_end_s"] - d["gap_start_s"], 300.0)
        _, _, d = M.corrupt_cut_version(subs, random.Random("x"))
        self.assertEqual(d["cut_end_s"] - d["cut_start_s"], 300.0)



def _pts(*pairs):
    """(audio_s, shift_s) -> one sample carrying raw anchor_points."""
    return [{"start": 0.0, "anchor": None,
             "anchor_points": [(a, a - sh) for a, sh in pairs]}]


class PointRunTests(unittest.TestCase):
    def test_six_lines_at_one_offset_are_a_block(self):
        base = [(t, 0.1) for t in range(0, 600, 20)]
        block = [(1000 + 15 * i, -4.4) for i in range(6)]
        runs = C.anchor_point_runs(_pts(*(base + block)))
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["dev"], -4.5, delta=0.2)

    def test_healthy_spread_is_not(self):
        # Healthy SH peaks at 1.43s over 6 lines.
        base = [(t, 0.1) for t in range(0, 600, 20)]
        wobble = [(1000 + 15 * i, -1.4) for i in range(6)]
        self.assertEqual(C.anchor_point_runs(_pts(*(base + wobble))), [])

    def test_scattered_mismatches_are_not(self):
        # Single wrong matches alternate with good lines: no run.
        pts = [(t, 0.1 if i % 2 else -6.0) for i, t in enumerate(range(0, 400, 20))]
        pts += [(t, 0.1) for t in range(400, 1200, 20)]
        self.assertEqual(C.anchor_point_runs(_pts(*pts)), [])

    def test_three_lines_are_too_few(self):
        # A 6-wide median needs 4 of 6 off to move.
        base = [(t, 0.1) for t in range(0, 600, 20)]
        block = [(1000 + 15 * i, -4.4) for i in range(3)]
        self.assertEqual(C.anchor_point_runs(_pts(*(base + block))), [])


if __name__ == "__main__":
    unittest.main()
