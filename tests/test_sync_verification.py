"""End-to-end sync/correctness verification against REAL Whisper data, at zero ongoing API cost.

Uses tests/sync_verification.py's WhisperShim to serve every clip the app's own sampling logic
asks for (collect_samples, correctness_check) by slicing a full-episode transcript that was
really transcribed once (tests/build_whisper_fixtures.py) -- alass itself is NOT mocked, it runs
for real, locally, against the real video file, exactly as a live sweep would. This tests the
actual call PATTERN a normal run makes (same functions, same positions, same DB caching), not a
synthetic stand-in for it.

Ground truth for "did this come out right" is tests/sync_verification.GroundTruth -- built by
matching the real subtitle's own text against the full transcript ACROSS THE WHOLE EPISODE
(dozens to ~90 confident anchors per episode here), independent of and much denser than anything
the app itself samples -- so a scenario's assertion is never just "the app agrees with itself".

Requires: tests/fixtures/whisper_full/{S02E01,S02E06,S02E10}.json to exist (see
build_whisper_fixtures.py) and the real video files to still be at sync_verification.MEDIA_DIR
(alass needs the actual audio). Does NOT require an STT API key -- the shim never makes a real
HTTP call.

Run: python3 -m unittest tests.test_sync_verification -v
     (or: python3 -m unittest discover -s tests -v -- picked up alongside the generate.py tests)
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verifyarr import db, pipeline
from verifyarr.correctness import significant_anchor_residuals
from verifyarr.subtitles import (anchor_points, anchor_regions, plan_anchor_resync,
                                 apply_anchor_resync)
import pysubs2
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs

from sync_verification import (
    FIXTURES_DIR, MEDIA_DIR, load_fixture, fixture_paths, build_ground_truth, patch_whisper,
)

# Episodes with a healthy, densely-anchored, low-residual ORIGINAL subtitle -- a clean enough
# baseline to inject KNOWN synthetic breaks into and check the app corrects exactly that break
# (see the exploratory session that built these fixtures). S02E15's subtitle was replaced on
# disk after that session (the original .en.hi.srt, corrupted at the very start, was joined by
# a clean .en.srt -- see the fixtures README) and now qualifies too.
#
# S02E21's subtitle was ALSO replaced, and is NOT here: its new .en.srt is internally
# consistent and matches Whisper closely for most of the episode (24% ground-truth coverage,
# median residual 0.3s) but has a real, substantial pre-existing drift of its own in roughly
# the back third (a tight ~54s cluster from ~692-802s, then a tight ~41s cluster from
# ~840-1182s -- too consistent within each cluster to be anchor-matching noise) -- exactly the
# kind of already-broken input that would confound a scenario meant to test a KNOWN, injected
# break (see the original audit's S02E03 finding, which hit the same confound). Used instead in
# RealWorldFixTests below, unmodified, as a genuine "does the app fix a real problem" case.
HEALTHY_SLUGS = ["S02E01", "S02E06", "S02E10", "S02E15"]

SCRATCH_DIR = Path(tempfile.gettempdir()) / "verifyarr-sync-verification"


def _test_config(conn) -> Config:
    """A Config built from real settings.py defaults (via an empty scratch DB, so every
    threshold is whatever this app ships with, not whatever the user's own install happens to
    have tuned) with just enough overridden to exercise the sync/correctness/anchor code paths
    without ever needing a real API key -- the shim intercepts every call before it would reach
    the network, so active_stt_api_key only has to be non-empty to pass the app's own
    "is a key configured" gates."""
    cfg = Config.from_db(conn)
    for k, v in dict(
        groq_api_key="shim-no-real-key-needed", stt_provider="groq",
        backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, anchor_check_enabled=True,
        line_order_enabled=True, line_order_audio_confirm=True,
    ).items():
        object.__setattr__(cfg, k, v)
    return cfg


def _prime_full_cache(conn, cfg, fixtures) -> None:
    """Pre-populate the full-transcript cache from fixtures -- the same pattern
    verifyarr_handoff/e2e_after.py's run_one uses. Anything that escalates to a
    full-track transcript (notably the sampled->full path) then reads real,
    previously-transcribed segments instead of needing a live STT call."""
    for slug, fx in fixtures.items():
        video_path, _ = fixture_paths(fx)
        db.save_full_transcript_cache(conn, video_path, fx["language"], fx["segments"],
                                      stt_provider=cfg.stt_provider, stt_model=cfg.groq_model)



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


class SyncVerificationCase(unittest.TestCase):
    """One instance per (episode, scenario). Subclasses/instances are generated in bulk at
    module load time (see _make_case below) rather than hand-written per episode, so adding a
    4th healthy fixture to HEALTHY_SLUGS automatically multiplies test coverage instead of
    requiring new test methods."""

    @classmethod
    def setUpClass(cls):
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        cls.conn = db.connect(SCRATCH_DIR / "scratch.db")
        cls.cfg = _test_config(cls.conn)
        cls.fixtures = {slug: load_fixture(slug) for slug in HEALTHY_SLUGS}
        cls.ground_truths = {slug: build_ground_truth(f) for slug, f in cls.fixtures.items()}

    def _run(self, slug: str, subs, lang: str = "en"):
        """sync_pair + correctness_and_finish against a scratch copy of `subs`, under the
        Whisper shim -- returns (row, final_subs_on_disk, shim). Mirrors exactly what
        pipeline.process_pair does for one real file in a real sweep."""
        fixture = self.fixtures[slug]
        video_path, _ = fixture_paths(fixture)
        tmp = SCRATCH_DIR / f"{slug}_{self._testMethodName}.srt"
        subs.save(str(tmp))
        with patch_whisper(fixture) as shim:
            row, current_subs = pipeline.sync_pair(video_path, tmp, lang, self.cfg, {}, SCRATCH_DIR)
            row = pipeline.correctness_and_finish(video_path, tmp, lang, self.cfg, self.conn, row, current_subs)
        json.dumps(row)  # every scenario implicitly checks the row survives serialization
        return row, load_subs(tmp), shim


def _shift(subs, seconds: float):
    out = copy.deepcopy(subs)
    out.shift(s=seconds)
    return out


def _partial_shift(subs, seconds: float, fraction: float = 0.5):
    out = copy.deepcopy(subs)
    cutoff = out.events[int(len(out.events) * fraction)].start
    for e in out.events:
        if e.start >= cutoff:
            e.shift(s=seconds)
    return out


def _swap_lines(subs, indices):
    from verifyarr.line_order import _split_two_lines
    out = copy.deepcopy(subs)
    for i in indices:
        e = out.events[i]
        parts = _split_two_lines(e.text)
        if parts:
            l1, l2 = parts
            e.text = f"{l2}\\N{l1}"
    return out


class CleanFileTests(SyncVerificationCase):
    def test_unmodified_subtitle_is_recognized_as_already_synced(self):
        for slug in HEALTHY_SLUGS:
            with self.subTest(slug=slug):
                subs = load_subs(fixture_paths(self.fixtures[slug])[1])
                row, _final, shim = self._run(slug, subs)
                self.assertEqual(row["sync_status"], "already in sync", row["note"])
                self.assertEqual(row["correctness_flag"], "ok", row["note"])
                # a clean file must never trip the anchor-residual escalation
                self.assertNotIn("Escalated to SUSPECT", row["note"])


class GlobalShiftTests(SyncVerificationCase):
    def test_small_shift_is_corrected(self):
        for slug in HEALTHY_SLUGS:
            with self.subTest(slug=slug):
                original = load_subs(fixture_paths(self.fixtures[slug])[1])
                broken = _shift(original, 3.0)
                row, final, _shim = self._run(slug, broken)
                self.assertTrue(row["sync_status"].startswith("fixed"), row["sync_status"])
                summ = self.ground_truths[slug].summary_for(final)
                self.assertIsNotNone(summ)
                self.assertLess(summ["median_abs_residual"], 1.5, summ)

    def test_large_shift_is_corrected(self):
        # Positive, not negative: a large enough NEGATIVE shift pushes early events' start times
        # below zero, and pysubs2 clamps negative timestamps to 00:00:00,000 when serializing to
        # SRT (verified separately -- see test_extreme_negative_shift_reveals_a_real_bug below),
        # corrupting the very input this scenario means to test. +45s has no such edge case.
        for slug in HEALTHY_SLUGS:
            with self.subTest(slug=slug):
                original = load_subs(fixture_paths(self.fixtures[slug])[1])
                broken = _shift(original, 45.0)
                row, final, _shim = self._run(slug, broken)
                self.assertTrue(row["sync_status"].startswith("fixed"), row["sync_status"])
                summ = self.ground_truths[slug].summary_for(final)
                self.assertIsNotNone(summ)
                self.assertLess(summ["median_abs_residual"], 1.5, summ)
                self.assertEqual(row["correctness_flag"], "ok", row["note"])


class PartialShiftTests(SyncVerificationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _prime_full_cache(cls.conn, cls.cfg, cls.fixtures)
    """The scenario that motivated pipeline._resolve_ambiguous_sync's whole design (and caught
    3 real bugs in it during the audit session that built this suite) -- a genuine two-part
    discontinuity, which only alass's multi-block fit can correct in full; a plain single
    global offset is mathematically guaranteed wrong for at least one half."""

    def test_second_half_shift_is_fully_corrected_by_the_block_fit(self):
        for slug in HEALTHY_SLUGS:
            with self.subTest(slug=slug):
                original = load_subs(fixture_paths(self.fixtures[slug])[1])
                broken = _partial_shift(original, 18.0, fraction=0.5)
                row, final, _shim = self._run(slug, broken)
                gt = self.ground_truths[slug]
                residuals = gt.residuals_for(final)
                n = len(final.events)
                first = [r for i, r in residuals if i < n // 2]
                second = [r for i, r in residuals if i >= n // 2]
                self.assertTrue(first, "no ground-truth coverage in the first half")
                self.assertTrue(second, "no ground-truth coverage in the second half")
                import statistics
                # Same tolerance the app's own anchor-escalation uses (subtitles.
                # ANCHOR_SUSPECT_THRESHOLD_S) -- not a tighter, arbitrary one: real Whisper
                # segment-start jitter against a genuinely correct cue routinely reaches 1-2s
                # (see the module docstring's approximation note), so holding this scenario to
                # a stricter bar than the app itself uses to decide "is this actually wrong"
                # would fail on ordinary noise, not on a real defect.
                self.assertLess(statistics.median(map(abs, first)), 2.5,
                                f"{slug}: first half not corrected: {first}")
                self.assertLess(statistics.median(map(abs, second)), 2.5,
                                f"{slug}: second half not corrected: {second}")


class WrongEpisodeTests(SyncVerificationCase):
    def test_a_different_episodes_subtitle_is_flagged_suspect(self):
        # E06's subtitle text (only) against E01's video/audio, and vice versa -- alass still
        # "fits" some rhythm (blind to content), so this exercises the CONTENT check, not sync.
        pairs = [("S02E01", "S02E06"), ("S02E06", "S02E01")]
        for video_slug, text_slug in pairs:
            with self.subTest(video=video_slug, text_from=text_slug):
                wrong_subs = load_subs(fixture_paths(self.fixtures[text_slug])[1])
                row, final, _shim = self._run(video_slug, wrong_subs)
                self.assertEqual(row["correctness_flag"], "SUSPECT", row["note"])
                # Re-timing text that isn't this episode's is meaningless, and writing it
                # destroys the original (backup_originals can be off). alass DOES produce a
                # confident-looking fit here -- it only fits rhythm, it cannot see that the
                # words are wrong -- so the content check is the only thing standing between a
                # wrong subtitle and an overwritten file. See _resolve_ambiguous_sync's
                # no-content-match branch, and the real C_S02E15 case that motivated it (its
                # on-disk subtitle is the NEXT episode's text; the pipeline used to "fix" it by
                # 30.4s on the strength of a 0.09 score).
                starts_before = [e.start for e in wrong_subs.events]
                starts_after = [e.start for e in final.events]
                self.assertEqual(starts_after, starts_before,
                                 f"{video_slug}: a wrong-episode subtitle was re-timed instead "
                                 f"of being left alone ({row['sync_status']})")


class LineOrderTests(SyncVerificationCase):
    def test_swapped_lines_are_detected_against_real_audio(self):
        from verifyarr.line_order import heuristic_candidates
        from verifyarr.line_order import _split_two_lines, _cap_signal
        found_any = False
        for slug in HEALTHY_SLUGS:
            original = load_subs(fixture_paths(self.fixtures[slug])[1])
            # collect_samples only ever tests a candidate through the (free) heuristic PRE-
            # FILTER -- _cap_signal (L1 lowercase-start, L2 uppercase-start, unless L1 ends a
            # sentence). Swapping two lines that are already both lowercase- or both
            # uppercase-start (common -- most of a script's lines are mid-sentence fragments)
            # produces a swap the heuristic itself would never flag, so collect_samples would
            # never sample it at all -- not a detection failure, just an untested candidate
            # (confirmed against S02E15: picking the first few multi-line events regardless of
            # this gave 3 candidates whose swap NEVER became heuristic-eligible, and the
            # scenario correctly found nothing because there was nothing TO find). Select only
            # candidates whose SWAPPED form would actually trip _cap_signal, so this scenario
            # tests real detection, not an artifact of which lines happened to get picked.
            candidates = []
            for i, e in enumerate(original.events):
                parts = _split_two_lines(e.text)
                if parts and _cap_signal(parts[1], parts[0]):
                    candidates.append(i)
            targets = [i for i in candidates if i not in {c[0] for c in heuristic_candidates(original)}][:3]
            if not targets:
                continue
            found_any = True
            with self.subTest(slug=slug):
                broken = _swap_lines(original, targets)
                row, _final, _shim = self._run(slug, broken)
                # Either individually auto-fixed (line_order_fixed > 0) or, if a majority of
                # TESTED candidates end up swapped, escalated to a file-level SUSPECT -- both
                # are the app correctly noticing the swap; only total silence is a failure.
                noticed = (row.get("line_order_fixed") or 0) > 0 or row["correctness_flag"] == "SUSPECT"
                self.assertTrue(noticed, f"{slug}: swapped lines at {targets} went unnoticed: {row}")
        self.assertTrue(found_any, "no eligible 2-line events found to swap in any healthy fixture")


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


class KnownEdgeCaseTests(SyncVerificationCase):
    def test_extreme_negative_shift_clamps_to_zero_on_save(self):
        """Not a claim about this app's own code -- a recorded fact about pysubs2 (the SRT
        library every part of this app reads/writes through) that explains a real anomaly found
        while building these fixtures: S02E15 and S02E21's ORIGINAL downloaded .srt files (since
        replaced on disk with clean ones -- see HEALTHY_SLUGS and RealWorldFixTests) each had
        their first 14-16 cues stuck at exactly 00:00:00,000, with normal, increasing timestamps
        for the rest of the file. Reproduced here directly: shifting a real subtitle
        EARLIER by more than its first cue's own start time, then saving it back to .srt (the
        exact shape of what an over-correcting auto-sync tool -- this app included, if a bug
        ever computed too large a NEGATIVE shift -- would do), clamps every now-negative
        timestamp to zero instead of erroring, silently producing exactly the same corruption
        pattern. Kept as a standing regression check on pysubs2's behavior (and a reason
        GlobalShiftTests uses +45s, never a negative shift large enough to cross zero) rather
        than as a bug report against this app -- alass's own SRT writer, and every SRT this app
        itself ever writes via _write_fix, never has to represent a negative timestamp in the
        first place."""
        original = load_subs(fixture_paths(self.fixtures["S02E01"])[1])
        first_start_sec = original.events[0].start / 1000.0
        broken = _shift(original, -(first_start_sec + 30.0))  # guaranteed to cross zero
        tmp = SCRATCH_DIR / "negative_clamp_probe.srt"
        broken.save(str(tmp))
        reloaded = load_subs(tmp)
        self.assertEqual(reloaded.events[0].start, 0)
        self.assertEqual(reloaded.events[0].end, 0)


class StructuralChangeTests(SyncVerificationCase):
    """row["structural_change"] is a narrower check than its name suggests -- it does NOT
    compare a subtitle against any expectation of how many lines an episode "should" have; it
    only compares the file already on disk against ALASS'S OWN retimed output for that SAME
    file (see pipeline.sync_pair: old_n = the input's own event count, new_n = alass's output's
    event count -- max_shift_stats' callers). alass practically never adds or removes cues, it
    only retimes them, so this flag is a narrow safety net for a malformed/truncated ALASS
    OUTPUT specifically, not a general "this subtitle is missing content" detector -- deleting
    events from the INPUT before alass ever sees it (tried first here) changes nothing this
    flag looks at, since both old_n and new_n shrink together. Not exercisable through this
    suite's tools (there is no practical way to make alass itself change an event count on
    demand), so there is no scenario test for it here -- this class exists to record why, for
    the next person who reaches for the obvious "delete some lines" test and wonders why it
    doesn't fire."""


class RealWorldFixTests(SyncVerificationCase):
    """S02E21's subtitle, exactly as it sits on disk -- no synthetic break injected. Unlike
    every other class here, the "expected" outcome isn't something this test constructed; it's
    what build_ground_truth found: the file matches Whisper closely for most of its length but
    has a real, substantial pre-existing drift in roughly its back third (two internally-tight
    clusters, ~54s then ~41s -- see HEALTHY_SLUGS' comment) that does not reduce to a clean,
    small number of alass-fittable blocks (alass's own split-penalty fit finds FIVE blocks here,
    not the two or three a textbook "two scenes swapped" case would produce) -- a genuinely
    harder file than anything this suite constructs on purpose.

    First run against this file (before the fix below existed) surfaced a real bug of its own:
    _resolve_ambiguous_sync picked 'old' -- leave the file completely untouched -- because its
    only 2 confident anchor clips both happened to land in the file's one genuinely fine
    stretch, and a WRONG candidate never produces a bad anchor in a region it's wrong about, it
    produces no anchor there at all (see pipeline._old_wins_fairly), so those two lucky clips
    were treated as proof rather than as the coincidence they were. Fixed by requiring 'old'
    clear the same per-block confirmation bar 'new' already had to.

    With that fixed, THIS file still isn't one alass can cleanly auto-correct -- its own 5-block
    fit reduces the error a lot but doesn't fully fix it (ground truth: median residual goes
    from ~1.3s "before" -- misleadingly low, dragged down by the large unbroken majority of the
    file -- to a real per-anchor picture that's still off in places after the fix). The
    achievable, meaningful bar for a file this messy isn't "perfectly fixed"; it's "the app
    doesn't silently call it fine" -- and the anchor-residual escalation (Config.
    anchor_check_enabled) is exactly the safety net for that: a confident anchor still showing a
    real mismatch after the fix escalates the whole file to SUSPECT even though the majority-
    vote average passed, handing it to Bazarr/a human instead of quietly leaving it wrong.

    NOTE: that "not cleanly auto-correctable" premise is now only true of THIS test's
    configuration. These cases run in the default sampled mode, where three samples can't produce
    the three agreeing anchors per region an anchor resync needs. In whisper_mode="full" the same
    file IS corrected -- pipeline._try_anchor_resync plans three regions from its dense anchors and
    takes it from 53.3s out at its worst point to 2.5s, measured against an independent
    large-v3-turbo reference. The assertions below still hold either way: they check that the
    drift is present in the FIXTURE, not that the app fails to fix it."""

    @classmethod
    def setUpClass(cls):
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        cls.conn = db.connect(SCRATCH_DIR / "scratch_e21.db")
        cls.cfg = _test_config(cls.conn)
        cls.fixtures = {"S02E21": load_fixture("S02E21")}
        _prime_full_cache(cls.conn, cls.cfg, cls.fixtures)
        cls.ground_truths = {"S02E21": build_ground_truth(cls.fixtures["S02E21"])}

    def test_real_pre_existing_drift_is_detected(self):
        gt = self.ground_truths["S02E21"]
        original = gt.original_subs
        before = gt.summary_for(original)
        self.assertIsNotNone(before)
        self.assertGreater(before["max_abs_residual"], 30,
                            "expected the known real drift to still be present in the fixture")

        row, final, _shim = self._run("S02E21", original)
        # 'old' (untouched) must never be the answer for a file alass itself measured a large
        # multi-block spread on -- see this class's own docstring for the real bug that once
        # made that happen.
        self.assertNotIn("rejected", row["sync_status"], row["note"])
        after = gt.summary_for(final)
        self.assertIsNotNone(after)
        # A genuinely hard file: don't require a perfect fix, only that alass's attempt made
        # real progress (nowhere close to leaving the ~54s/~41s drift untouched)...
        self.assertLess(after["max_abs_residual"], before["max_abs_residual"] * 0.75, after)
        # ...and, critically, that whatever residual problem remains was NOT silently accepted:
        # the file must be flagged, not reported "ok".
        self.assertEqual(row["correctness_flag"], "SUSPECT", row["note"])


if __name__ == "__main__":
    unittest.main()
