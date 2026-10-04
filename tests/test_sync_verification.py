"""Unit tests for the anchor, resync-planning, cache-key and local-Whisper-JSON helpers.

Everything here is self-contained (inline subtitles and synthetic anchors). The pipeline-level checks that used
to live here ran alass on Community episodes from a local folder; they are replaced by the Known Good tests
(test_clean_files, test_silent_rows, test_swap_gate, test_screen_order, ...), which run the same cases from the repo.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pysubs2
from verifyarr import db, pipeline
from verifyarr.correctness import significant_anchor_residuals, full_transcript_cache_key
from verifyarr.settings import Config
from verifyarr.subtitles import (anchor_points, anchor_regions, plan_anchor_resync,
                                 apply_anchor_resync, load_subs)


def _test_config(conn) -> Config:
    """A Config built from the shipped settings defaults (empty scratch DB) with a few switches turned on."""
    cfg = Config.from_db(conn)
    for k, v in dict(
        local_whisper_binary=sys.executable, backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, anchor_check_enabled=True,
        line_order_enabled=True, line_order_audio_confirm=True,
    ).items():
        object.__setattr__(cfg, k, v)
    return cfg


class AnchorEscalationThresholdTests(unittest.TestCase):
    """significant_anchor_residuals' minimum count. Measured over 52 real episodes: every FALSE
    escalation had 1-2 flagged samples and every true one had 3-14, so the count separates them
    where the magnitude does not (a real 3.4s residual and a false 4.8s one both occur)."""

    @staticmethod
    def _samples(shifts):
        return [{"start": float(i * 60), "anchor": {"shift": v, "mad": 0.1, "anchor_count": 4}}
                for i, v in enumerate(shifts)]

    def test_a_single_large_residual_is_not_enough(self):
        self.assertEqual(significant_anchor_residuals(self._samples([0.1, 0.2, -9.9])), [])

    def test_two_are_still_not_enough(self):
        self.assertEqual(significant_anchor_residuals(self._samples([0.1, -4.8, -2.8])), [])

    def test_three_escalate(self):
        bad = significant_anchor_residuals(self._samples([-19.4, -19.7, -19.1, 0.2]))
        self.assertEqual(len(bad), 3)

    def test_they_need_not_agree_in_sign(self):
        # a genuinely broken file can be off in both directions (C_S02E19: +54, +19, -24)
        bad = significant_anchor_residuals(self._samples([54.2, 18.9, -24.5]))
        self.assertEqual(len(bad), 3)

    def test_samples_without_a_confident_anchor_never_count(self):
        samples = self._samples([-19.0, -19.0]) + [{"start": 900.0, "anchor": None}]
        self.assertEqual(significant_anchor_residuals(samples), [])



class LineOrderCacheKeyTests(unittest.TestCase):
    """The cached collect_samples payload IS transcription output (samples, whisper_verdicts),
    so anything that changes WHICH model produced it has to change the key. Switching
    WHISPER_MODEL used to silently reuse the previous model's verdicts until the subtitle
    itself changed."""

    @staticmethod
    def _subs():
        import pysubs2
        f = pysubs2.SSAFile()
        f.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello there"))
        return f

    @staticmethod
    def _cfg(**over):
        # Real settings.py defaults via an empty scratch DB, same shape as _test_config above.
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            cfg = Config.from_db(conn)
            conn.close()
        for k, v in over.items():
            object.__setattr__(cfg, k, v)
        return cfg

    def _key(self, **over):
        from verifyarr.line_order import cache_key_for
        return cache_key_for(self._subs(), self._cfg(**over))

    def test_a_different_local_model_is_a_different_key(self):
        a = self._key(local_whisper_model="/m/ggml-tiny.en.bin")
        b = self._key(local_whisper_model="/m/ggml-small.en.bin")
        self.assertNotEqual(a, b)

    def test_the_same_settings_still_give_the_same_key(self):
        a = self._key(local_whisper_model="/m/ggml-tiny.en.bin")
        b = self._key(local_whisper_model="/m/ggml-tiny.en.bin")
        self.assertEqual(a, b)

    def test_it_agrees_with_the_full_transcript_cache_key(self):
        # One source for (provider, model) -- a reader and a writer can't disagree.
        cfg = self._cfg(local_whisper_model="/m/ggml-tiny.en.bin")
        provider, model = full_transcript_cache_key(cfg)
        from verifyarr.line_order import cache_key_for
        self.assertTrue(cache_key_for(self._subs(), cfg).endswith(f":{provider}:{model}"))


class AnchorResyncPlanningTests(unittest.TestCase):
    """The planner that turns anchors into an actual correction (subtitles.plan_anchor_resync).
    Every shape here is taken from a real file in the 52-episode corpus."""

    @staticmethod
    def _samples(pairs):
        return [{"start": float(t), "anchor": {"shift": v, "mad": 0.1, "anchor_count": 4}}
                for t, v in pairs]

    def _subs(self, n=80, step=15000, dur=4000):
        f = pysubs2.SSAFile()
        for i in range(n):
            f.append(pysubs2.SSAEvent(start=i * step, end=i * step + dur, text=f"line {i}"))
        return f

    def test_a_flat_healthy_file_gets_no_plan(self):
        pts = [(t, 0.2) for t in range(0, 1200, 60)]
        self.assertIsNone(plan_anchor_resync(self._subs(), self._samples(pts)))

    def test_two_clean_regions_are_found(self):
        # C_S03E14's shape: a mis-timed opening stretch, then the rest fine
        pts = [(t, 15.1) for t in range(0, 380, 20)] + [(t, 0.0) for t in range(400, 1260, 20)]
        regions = anchor_regions(anchor_points(self._samples(pts)))
        self.assertEqual(len(regions), 2)
        self.assertAlmostEqual(regions[0]["shift"], 15.1, places=1)
        self.assertAlmostEqual(regions[1]["shift"], 0.0, places=1)

    def test_a_lone_noisy_anchor_does_not_veto_the_plan(self):
        # C_S03E11: one -6.6s reading in an otherwise flat stretch
        pts = [(t, 0.1) for t in range(0, 660, 20)] + [(680, -6.6)] + \
              [(t, 0.1) for t in range(700, 960, 20)] + [(t, -18.5) for t in range(980, 1240, 20)]
        regions = anchor_regions(anchor_points(self._samples(pts)))
        self.assertIsNotNone(regions, "a single outlier should be absorbed, not fail the file")
        self.assertEqual(len(regions), 2)

    def test_a_continuously_drifting_file_is_refused(self):
        # C_S02E19: no run of agreeing anchors anywhere, so nothing can be planned
        pts = [(200, 37.2), (300, 21.7), (340, 14.8), (380, 5.6), (400, 2.0), (500, -1.1),
               (520, -2.8), (560, -11.8), (680, -31.0), (700, -34.4), (720, -38.8), (780, -36.3)]
        self.assertIsNone(anchor_regions(anchor_points(self._samples(pts))))

    def test_a_straddling_transition_anchor_is_dropped_not_vetoing(self):
        # was test_a_genuine_transition_with_too_little_evidence_is_refused: a clip window
        # covering the boundary measures the median of two offsets, i.e. a value strictly
        # between its disagreeing neighbours. That one anchor is dropped so the solid regions
        # on both sides can still be planned from -- real on C_S02E09 piecewise, where 6 clean
        # runs of 8-9 anchors were vetoed by one straddler per boundary. The plan is still
        # re-measured densely before anything is written.
        pts = [(t, 0.0) for t in range(0, 300, 20)] + [(320, -12.0)] + \
              [(t, -25.0) for t in range(340, 700, 20)]
        regions = anchor_regions(anchor_points(self._samples(pts)))
        self.assertIsNotNone(regions)
        self.assertEqual(len(regions), 2)
        self.assertAlmostEqual(regions[0]["shift"], 0.0, places=1)
        self.assertAlmostEqual(regions[1]["shift"], -25.0, places=1)

    def test_a_wild_thin_run_between_different_offsets_still_vetoes(self):
        # same shape, but the thin value is OUTSIDE both neighbours -- not a straddle, so
        # genuinely undecidable (noise or a third offset with no evidence): still None.
        pts = [(t, 0.0) for t in range(0, 300, 20)] + [(320, 14.0)] + \
              [(t, -25.0) for t in range(340, 700, 20)]
        self.assertIsNone(anchor_regions(anchor_points(self._samples(pts))))

    def test_several_runs_with_a_straddler_each_still_plan(self):
        # C_S02E09 piecewise shape: clean runs with one boundary-straddling anchor between
        # each pair must yield one region per run, not None.
        pts = [(t, -6.8) for t in range(0, 150, 20)] + [(160, 0.9)] + \
              [(t, 8.0) for t in range(220, 380, 20)] + [(400, -2.0)] + \
              [(t, -12.0) for t in range(420, 600, 20)]
        regions = anchor_regions(anchor_points(self._samples(pts)))
        self.assertIsNotNone(regions)
        self.assertEqual(len(regions), 3)
        self.assertAlmostEqual(regions[0]["shift"], -6.8, places=1)
        self.assertAlmostEqual(regions[1]["shift"], 8.0, places=1)
        self.assertAlmostEqual(regions[2]["shift"], -12.0, places=1)

    def test_applying_a_plan_moves_each_region_by_its_own_shift(self):
        subs = self._subs()
        pts = [(t, 10.0) for t in range(0, 300, 20)] + [(t, 0.0) for t in range(400, 1100, 20)]
        plan = plan_anchor_resync(subs, self._samples(pts))
        self.assertIsNotNone(plan)
        out = apply_anchor_resync(subs, plan)
        early = [e for e in out.events if e.start < 200_000]
        self.assertTrue(all(e.start % 15000 != 0 for e in early),
                        "the first region should have been shifted off its original grid")

    def test_a_plan_never_leaves_cues_overlapping(self):
        subs = self._subs()
        pts = [(t, 0.0) for t in range(0, 300, 20)] + [(t, -25.0) for t in range(400, 1100, 20)]
        plan = plan_anchor_resync(subs, self._samples(pts))
        out = apply_anchor_resync(subs, plan)
        for current, following in zip(out.events, out.events[1:]):
            self.assertLessEqual(current.end, following.start)

    def test_shifts_never_produce_a_negative_timestamp(self):
        subs = self._subs()
        pts = [(t, -400.0) for t in range(0, 1100, 20)]
        plan = plan_anchor_resync(subs, self._samples(pts))
        out = apply_anchor_resync(subs, plan)
        self.assertTrue(all(e.start >= 0 and e.end >= e.start for e in out.events))


class ResyncVerifiedTests(unittest.TestCase):
    """pipeline._resync_verified with anchor-less samples (resync4.log crash).

    A sample whose window holds only silence/music gets anchor=None -- normal and
    common -- and must be ignored as non-evidence, not crash the mean residual."""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.cfg = _test_config(db.connect(Path(cls._td.name) / "t.db"))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    @staticmethod
    def _s(t, shift=None):
        a = None if shift is None else {"shift": shift, "mad": 0.1, "anchor_count": 4}
        return {"start": float(t), "anchor": a}

    def test_none_anchors_are_ignored_not_crashed(self):
        plan = [{"cut_audio_s": None}]
        after = [self._s(100, 0.2), self._s(200, None), self._s(300, -0.1)]
        self.assertAlmostEqual(
            pipeline._resync_verified(plan, after, self.cfg, Path("t.srt")), 0.15)

    def test_all_none_anchors_verifies_nothing(self):
        plan = [{"cut_audio_s": None}]
        after = [self._s(100, None), self._s(200, None)]
        self.assertIsNone(pipeline._resync_verified(plan, after, self.cfg, Path("t.srt")))

    def test_cut_straddlers_still_excluded(self):
        plan = [{"cut_audio_s": 200.0}]
        after = [self._s(100, 0.1), self._s(205, 45.0), self._s(400, 0.1)]
        self.assertAlmostEqual(
            pipeline._resync_verified(plan, after, self.cfg, Path("t.srt")), 0.1)


class CoveredPositionsTests(unittest.TestCase):
    """_covered_positions drops VAD silence-skip (None) slots. Found when every
    sampled-mode gap run crashed collect_samples with "TypeError: '<=' not
    supported between float and NoneType": a primed clip cache gives a speech
    timeline, a silent region becomes a None slot, and the extra-target-range
    comparison ordered that None against float bounds. A silent slot sampled
    no audio, so it must count as covering nothing (the block then correctly
    gets its extra targeted sample)."""

    def test_none_slots_cover_nothing(self):
        from verifyarr.line_order import _covered_positions
        slots = [("heuristic", {"clip_start": 10.0, "clip_end": 20.0,
                                "items": []}, None),
                 ("filler", 100.0, (90.0, 200.0)),
                 ("filler", None, (200.0, 300.0))]
        self.assertEqual(_covered_positions(slots), [10.0, 100.0])

    def test_range_comparison_survives_silence(self):
        from verifyarr.line_order import _covered_positions
        covered = _covered_positions([("filler", None, (0.0, 100.0))])
        self.assertFalse(any(0.0 <= pos < 100.0 for pos in covered))


class Pysubs2ClampTests(unittest.TestCase):
    def test_extreme_negative_shift_clamps_to_zero_on_save(self):
        """A recorded fact about pysubs2, the library every SRT in this app goes through: shifting a
        subtitle earlier than its first cue and saving it clamps every negative time to 0 instead of
        raising. That is how a cue block ends up stacked at 00:00:00 (and why the alass-clamped cues have
        to be put back before a stretch fix, see pipeline._unclamp_alass_output)."""
        import pysubs2
        subs = pysubs2.SSAFile()
        for t in (2.0, 5.0, 40.0):
            subs.events.append(pysubs2.SSAEvent(start=int(t * 1000), end=int(t * 1000) + 1500, text="x"))
        subs.shift(s=-(2.0 + 30.0))
        tmp = Path(tempfile.mkdtemp(prefix="clamp_")) / "probe.srt"
        subs.save(str(tmp))
        reloaded = load_subs(tmp)
        self.assertEqual((reloaded.events[0].start, reloaded.events[0].end), (0, 0))
        self.assertEqual(reloaded.events[1].start, 0)
        self.assertEqual(reloaded.events[2].start, 8000)



class LocalWhisperJsonTests(unittest.TestCase):
    """load_whisper_json against output that is not valid UTF-8.

    Real and reproducible: base.en-greedy on SH_S01E05 writes a truncated multibyte
    sequence inside a hallucinated song lyric, identically on every run.
    """

    def _write(self, payload: bytes) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "clip.json"
        p.write_bytes(payload)
        return p

    def test_invalid_utf8_recovers_instead_of_raising(self):
        from verifyarr.correctness import load_whisper_json
        payload = ('{"transcription": [{"text": " ♪ poo-poo-poo').encode("utf-8") \
            + b"\x8f" + 'a ♪"}]}'.encode("utf-8")
        with self.assertRaises(UnicodeDecodeError):   # the byte really is invalid
            payload.decode("utf-8")
        data = load_whisper_json(self._write(payload))
        self.assertEqual(len(data["transcription"]), 1)
        self.assertIn("�", data["transcription"][0]["text"])

    def test_valid_utf8_is_unchanged(self):
        from verifyarr.correctness import load_whisper_json
        payload = '{"transcription": [{"text": " ♪ hej æøå ♪"}]}'.encode("utf-8")
        data = load_whisper_json(self._write(payload))
        self.assertEqual(data["transcription"][0]["text"], " ♪ hej æøå ♪")

    def test_malformed_json_still_raises(self):
        from verifyarr.correctness import load_whisper_json
        with self.assertRaises(RuntimeError):
            load_whisper_json(self._write(b'{"transcription": ['))


if __name__ == "__main__":
    unittest.main()
