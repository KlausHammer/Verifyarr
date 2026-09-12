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
                row, _final, _shim = self._run(video_slug, wrong_subs)
                self.assertEqual(row["correctness_flag"], "SUSPECT", row["note"])


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
    vote average passed, handing it to Bazarr/a human instead of quietly leaving it wrong."""

    @classmethod
    def setUpClass(cls):
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        cls.conn = db.connect(SCRATCH_DIR / "scratch_e21.db")
        cls.cfg = _test_config(cls.conn)
        cls.fixtures = {"S02E21": load_fixture("S02E21")}
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
