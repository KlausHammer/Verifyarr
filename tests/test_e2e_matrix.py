"""Focused offline tests for tests/e2e_matrix.py's pure helpers.

No video, no alass, no network: corruptions, sweep normalization, swap
scoring and resume bookkeeping only. The full pipeline runs live in
e2e_matrix.py itself, not here.
"""
from __future__ import annotations

import copy
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M


def _subs(texts):
    import pysubs2
    subs = pysubs2.SSAFile()
    t = 10_000
    for text in texts:
        subs.append(pysubs2.SSAEvent(start=t, end=t + 2000, text=text))
        t += 5000
    return subs


class SweepNormalizationTests(unittest.TestCase):
    def test_ms_offsets_become_seconds(self):
        doc = {"result": {"language": "en"}, "transcription": [
            {"timestamps": {"from": "00:00:00,000", "to": "00:00:06,900"},
             "offsets": {"from": 0, "to": 6900}, "text": " Why is everyone wearing hats? "},
            {"timestamps": {"from": "00:00:06,900", "to": "00:00:10,540"},
             "offsets": {"from": 6900, "to": 10540}, "text": "Is the Dean planning"},
        ]}
        segs = M.sweep_segments(doc)
        self.assertEqual(segs, [
            {"start": 0.0, "end": 6.9, "text": "Why is everyone wearing hats?"},
            {"start": 6.9, "end": 10.54, "text": "Is the Dean planning"},
        ])
        self.assertEqual(M.sweep_language(doc), "en")

    def test_empty_and_degenerate_entries_dropped(self):
        doc = {"transcription": [
            {"offsets": {"from": 0, "to": 1000}, "text": "   "},
            {"offsets": {"from": 5000, "to": 5000}, "text": "zero length"},
            {"text": "no offsets at all"},
            {"offsets": {"from": 1000, "to": 3000}, "text": "kept"},
        ]}
        self.assertEqual(M.sweep_segments(doc),
                         [{"start": 1.0, "end": 3.0, "text": "kept"}])

    def test_language_falls_back_to_en(self):
        self.assertEqual(M.sweep_language({}), "en")


class CorruptionTests(unittest.TestCase):
    def test_clean_changes_nothing(self):
        subs = _subs(["Hello world.", "Second line here."])
        out, kept, detail = M.corrupt_clean(subs, random.Random(0))
        self.assertIsNone(kept)
        self.assertEqual([e.text for e in out.events], [e.text for e in subs.events])
        self.assertEqual([e.start for e in out.events], [e.start for e in subs.events])

    def test_uniform_shifts_everything_45s(self):
        subs = _subs(["Hello world.", "Second line here."])
        out, kept, detail = M.corrupt_uniform(subs, random.Random(0))
        self.assertEqual(detail["shift_s"], 45.0)
        self.assertEqual(detail["dropped_before_zero"], 0)
        self.assertIsNone(kept, "a positive shift drops nothing, so there is nothing to map")
        for a, b in zip(out.events, subs.events):
            self.assertEqual(a.start - b.start, 45000)
            self.assertEqual(a.end - b.end, 45000)

    def test_negative_uniform_drops_cues_it_would_push_before_zero(self):
        """An SRT cannot hold a negative timestamp, so they are dropped, not clamped --
        clamping would turn a constant offset into a piecewise one at the head."""
        subs = _subs(["Hello world.", "Second line here."])
        out, kept, detail = M.corrupt_uniform(subs, random.Random(0), shift_s=-45.0)
        self.assertEqual(detail["shift_s"], -45.0)
        self.assertEqual(detail["dropped_before_zero"], len(subs.events) - len(out.events))
        self.assertTrue(all(e.start >= 0 for e in out.events))
        if detail["dropped_before_zero"]:
            self.assertEqual(len(kept), len(out.events))
            for j, i in enumerate(kept):
                self.assertEqual(out.events[j].start - subs.events[i].start, -45000)

    def test_pal_ratios_are_the_real_conversion(self):
        """25/24 = +4.167%, and it has to stay under STRETCH_MAX_RATE to be fixable."""
        from verifyarr.subtitles import STRETCH_MAX_RATE
        self.assertAlmostEqual(M.PAL_RATIO, 25 / 24.0)
        self.assertLess(M.PAL_RATIO - 1, STRETCH_MAX_RATE)
        subs = _subs(["a", "b", "c"])
        late, _, d_late = M.corrupt_pal_late(subs, random.Random(0))
        early, _, _ = M.corrupt_pal_early(subs, random.Random(0))
        self.assertAlmostEqual(d_late["ratio"], 1.041667, places=5)
        for a, b, c in zip(late.events, early.events, subs.events):
            self.assertGreater(a.start, c.start)
            self.assertLess(b.start, c.start)

    def test_drift_offset_carries_both_rate_and_intercept(self):
        """The only scenario where the fitted intercept is non-zero: corrupt_drift
        pivots at t=0, so on its own it never exercises that half of the fix."""
        subs = _subs(["a", "b", "c"])
        out, _, detail = M.corrupt_drift_offset(subs, random.Random(0))
        self.assertEqual((detail["rate"], detail["offset_s"]), (0.02, 8.0))
        for a, b in zip(out.events, subs.events):
            self.assertAlmostEqual(a.start, b.start * 1.02 + 8000, delta=2)
        # the gap between first and last error is the rate; the floor is the offset
        first = out.events[0].start - subs.events[0].start
        last = out.events[-1].start - subs.events[-1].start
        self.assertGreater(last, first)
        self.assertGreater(first, 7900)

    def test_cut_version_moves_what_missing_middle_leaves_alone(self):
        """The distinction the rename is about: both delete the middle, only one is a
        cut. A cut also pulls everything after it earlier."""
        subs = _subs([f"line {i}" for i in range(40)])
        cut, cut_kept, _ = M.corrupt_cut_version(subs, random.Random(0), cut_seconds=20.0)
        miss, miss_kept, _ = M.corrupt_missing_middle(subs, random.Random(0), gap_seconds=20.0)
        self.assertLess(len(cut.events), len(subs.events))
        # missing_middle: every survivor keeps its original time
        for j, i in enumerate(miss_kept):
            self.assertEqual(miss.events[j].start, subs.events[i].start)
        # cut_version: survivors after the cut are earlier, and none is negative
        moved = [j for j, i in enumerate(cut_kept)
                 if cut.events[j].start != subs.events[i].start]
        self.assertTrue(moved, "a cut must move the cues after it")
        for j in moved:
            self.assertLess(cut.events[j].start, subs.events[cut_kept[j]].start)
        self.assertTrue(all(e.start >= 0 for e in cut.events))

    def test_dropdup_kept_maps_duplicates_back_to_their_original(self):
        """A duplicate must score against the line it copies, not shift every later
        cue by one -- timing_errors pairs after_events[j] with ref_events[kept[j]]."""
        subs = _subs([f"line {i}" for i in range(200)])
        out, kept, detail = M.corrupt_dropdup(subs, random.Random(7), frac=0.05)
        self.assertEqual(len(kept), len(out.events))
        self.assertGreater(detail["dropped"], 0)
        self.assertGreater(detail["duplicated"], 0)
        self.assertEqual(len(out.events),
                         len(subs.events) - detail["dropped"] + detail["duplicated"])
        # every surviving timing is untouched, so the error against the reference is 0
        errs = M.timing_errors(list(subs.events), list(out.events), kept)
        self.assertEqual(max(errs), 0.0)

    def test_jitter_has_no_systematic_shape(self):
        """If it had one, a single offset would fix it and it would not be a noise test."""
        subs = _subs([f"line {i}" for i in range(300)])
        out, kept, _ = M.corrupt_jitter(subs, random.Random(3))
        self.assertIsNone(kept)
        deltas = [(a.start - b.start) / 1000.0 for a, b in zip(out.events, subs.events)]
        self.assertLess(abs(sum(deltas) / len(deltas)), 0.5, "jitter drifted one way")
        self.assertTrue(all(1.0 <= abs(d) <= 3.0 for d in deltas[1:]))

    def test_swap_targets_are_spread_not_clumped(self):
        """First-n-in-file-order put every swap in the opening minutes."""
        subs = _subs(["ALICE\\NThat is mine.", "BOB\\NNo it is not."] * 60)
        _, _, detail = M.corrupt_swap(subs, random.Random(0), n=6)
        targets = detail["swapped"]
        if len(targets) >= 6:
            self.assertGreater(max(targets), len(subs.events) // 2,
                               "no swap landed in the second half of the file")

    def test_every_default_scenario_is_registered_and_sane(self):
        """A name in DEFAULT_SCENARIOS that is not in SCENARIOS fails only at row 1 of a
        long run; and a corruption that emits a negative or inverted cue corrupts the
        measurement rather than the file."""
        subs = _subs([f"line {i}" for i in range(120)])
        for name in M.DEFAULT_SCENARIOS:
            self.assertIn(name, M.SCENARIOS, name)
            if name == "wrong_episode":
                continue   # needs real fixtures, covered by the matrix itself
            out, kept, _ = M.SCENARIOS[name](subs, random.Random(f"t:{name}"))
            with self.subTest(scenario=name):
                self.assertTrue(all(e.start >= 0 and e.end > e.start for e in out.events))
                if kept is not None:
                    self.assertEqual(len(kept), len(out.events))
                    self.assertTrue(all(0 <= i < len(subs.events) for i in kept))

    def test_drift_stretches_progressively(self):
        subs = _subs(["Hello world.", "Second line here."])
        out, _, detail = M.corrupt_drift(subs, random.Random(0), rate=0.02)
        self.assertEqual(detail, {"rate": 0.02})
        for a, b in zip(out.events, subs.events):
            self.assertEqual(a.start, int(b.start * 1.02))

    def test_gap_removes_middle_chunk(self):
        subs = _subs([f"line {i}" for i in range(40)])
        # Spread over >5 min so a 5-min middle gap removes some but not all.
        for k, e in enumerate(subs.events):
            e.start = k * 30_000
            e.end = e.start + 2000
        out, kept, detail = M.corrupt_missing_middle(subs, random.Random(0))
        self.assertTrue(0 < detail["removed_lines"] < 40)
        self.assertEqual(len(out.events), len(kept))
        self.assertEqual([subs.events[i].text for i in kept],
                         [e.text for e in out.events])

    def test_swap_reverses_only_eligible_cues(self):
        # [0]: swapped form trips _cap_signal -> eligible.
        # [1]: already heuristic-flagged in original -> excluded.
        # [2]: single line -> untouched.
        subs = _subs(["Then she stayed\\Nhe left early",
                      "he left early\\NShe stayed on",
                      "Just one line here."])
        out, _, detail = M.corrupt_swap(subs, random.Random(0))
        self.assertEqual(detail["swapped"], [0])
        self.assertEqual(out.events[0].text, "he left early\\NThen she stayed")
        self.assertEqual(out.events[1].text, subs.events[1].text)
        self.assertEqual(out.events[2].text, subs.events[2].text)
        # Timings untouched by a swap.
        self.assertEqual([e.start for e in out.events], [e.start for e in subs.events])

    def test_swap_is_deterministic(self):
        subs = _subs(["Then she stayed\\Nhe left early"] * 10)
        a = M.corrupt_swap(copy.deepcopy(subs), random.Random(1))[2]
        b = M.corrupt_swap(copy.deepcopy(subs), random.Random(999))[2]
        self.assertEqual(a, b)


class SwapScoringTests(unittest.TestCase):
    def test_frac_restored(self):
        ref = _subs(["aaa\\Nbbb", "ccc"])
        after = _subs(["aaa\\Nbbb", "ccc"])  # fully restored
        self.assertEqual(M.swap_recovery(list(ref.events), list(after.events), [0]),
                         {"n_swapped": 1, "n_restored": 1, "frac_restored": 1.0})
        still = _subs(["bbb\\Naaa", "ccc"])  # unrestored
        self.assertEqual(M.swap_recovery(list(ref.events), list(still.events), [0])["frac_restored"], 0.0)

    def test_no_swaps_is_zero(self):
        ref = _subs(["ccc"])
        self.assertEqual(M.swap_recovery(list(ref.events), list(ref.events), []),
                         {"n_swapped": 0})


class ResumeTests(unittest.TestCase):
    def test_done_skips_completed_but_retries_skipped(self):
        import tempfile
        rows = [
            {"status": "ok", "model": "turbo", "slug": "C_S02E02", "scenario": "clean"},
            {"status": "error", "model": "turbo", "slug": "C_S02E02", "scenario": "swap"},
            {"status": "skipped", "model": "tiny.en-cpu", "slug": "C_S02E02", "scenario": "clean"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            done = M.rows_done(p)
        self.assertIn(("turbo", "C_S02E02", "clean", "full", "on"), done)
        self.assertIn(("turbo", "C_S02E02", "swap", "full", "on"), done)
        self.assertNotIn(("tiny.en-cpu", "C_S02E02", "clean", "full", "on"), done)

    def test_missing_file_is_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(M.rows_done(Path(td) / "nope.jsonl"), set())

    def test_redo_drops_only_owned_selection(self):
        import tempfile
        rows = [
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "clean"},
            {"status": "ok", "model": "turbo", "slug": "B", "scenario": "clean"},
            {"status": "skipped", "model": "tiny.en-cpu", "slug": "A", "scenario": "clean"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            kept, done = M.load_prior_rows(p, ["turbo"], ["A"], ["clean"], None, None, False)
            self.assertEqual(len(kept), 3)
            self.assertEqual(done, {("turbo", "A", "clean", "full", "on"),
                                    ("turbo", "B", "clean", "full", "on")})
            kept, done = M.load_prior_rows(p, ["turbo"], ["A"], ["clean"], None, None, True)
            self.assertEqual([(r["model"], r["slug"]) for r in kept],
                             [("turbo", "B"), ("tiny.en-cpu", "A")])
            self.assertEqual(done, {("turbo", "B", "clean", "full", "on")})

    def test_axes_are_part_of_resume_key(self):
        import tempfile
        rows = [
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "clean",
             "mode": "full", "audio_confirm": "on"},
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "clean",
             "mode": "sampled", "audio_confirm": "on"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            kept, done = M.load_prior_rows(p, ["turbo"], ["A"], ["clean"],
                                           ["sampled"], ["on"], True)
            # Only the sampled row is owned by this selection.
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["mode"], "full")
            self.assertEqual(done, {("turbo", "A", "clean", "full", "on")})

    def test_row_key_defaults_to_old_fixed_setup(self):
        self.assertEqual(M.row_key({"model": "m", "slug": "s", "scenario": "c"}),
                         ("m", "s", "c", "full", "on"))

    def test_dedupe_prefers_completed_over_stale_skipped(self):
        rows = [
            {"status": "skipped", "model": "m", "slug": "s", "scenario": "c",
             "mode": "full", "audio_confirm": "on", "reason": "no sweep data"},
            {"status": "ok", "model": "m", "slug": "s", "scenario": "c",
             "mode": "full", "audio_confirm": "on"},
            {"status": "skipped", "model": "m", "slug": "s", "scenario": "d",
             "mode": "full", "audio_confirm": "on", "reason": "no sweep data"},
        ]
        out = M.dedupe_rows(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[1]["status"], "skipped")


class SummaryTests(unittest.TestCase):
    def test_aggregates(self):
        rows = [
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "clean",
             "recovered": {"n": 10, "p50": 0.1, "frac_le_1_0s": 1.0}, "untouched": True},
            {"status": "ok", "model": "turbo", "slug": "B", "scenario": "clean",
             "recovered": {"n": 10, "p50": 0.3, "frac_le_1_0s": 0.9}, "untouched": False},
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "swap",
             "swap": {"n_swapped": 2, "n_restored": 1, "frac_restored": 0.5},
             "swap_noticed": True},
            {"status": "skipped", "model": "tiny.en-cpu", "slug": "A", "scenario": "clean",
             "reason": "no sweep data"},
        ]
        s = M.build_summary(rows, ["turbo", "tiny.en-cpu"], ["A", "B"], ["clean", "swap"])
        self.assertEqual(s["by_scenario"]["clean"]["untouched"], 1)
        self.assertEqual(s["by_scenario"]["clean"]["mean_p50"], 0.2)
        self.assertEqual(s["by_scenario"]["swap"]["mean_frac_restored"], 0.5)
        self.assertEqual(s["by_model"]["tiny.en-cpu"]["skipped"], 1)
        self.assertEqual(s["by_model"]["turbo"]["clean_untouched"], 1)


class SampledSliceTests(unittest.TestCase):
    def test_start_inside_selection_and_rebasing(self):
        segs = [
            {"start": 5.0, "end": 9.0, "text": "before"},     # ends inside, start outside
            {"start": 10.0, "end": 12.0, "text": "first"},
            {"start": 69.0, "end": 75.0, "text": "overhang"},  # starts inside, ends outside
            {"start": 70.0, "end": 71.0, "text": "outside"},
        ]
        clip = M.slice_clip_segments(segs, 10.0, 60.0)
        self.assertEqual([s["text"] for s in clip], ["first", "overhang"])
        self.assertEqual((clip[0]["start"], clip[0]["end"]), (0.0, 2.0))
        self.assertEqual((clip[1]["start"], clip[1]["end"]), (59.0, 65.0))

    def test_degenerate_entries_dropped(self):
        segs = [
            {"start": 11.0, "end": 11.0, "text": "zero"},
            {"start": 12.0, "end": 10.0, "text": "backwards"},
            {"text": "no times"},
            {"start": 13.0, "end": 14.0, "text": "kept"},
        ]
        self.assertEqual([s["text"] for s in M.slice_clip_segments(segs, 10.0, 60.0)],
                         ["kept"])


class CfgAxisTests(unittest.TestCase):
    def _cfg(self, *args, **kw):
        import tempfile
        from verifyarr import db
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            cfg = M.cfg_for(conn, *args, **kw)
            conn.close()
            return cfg

    def test_sampled_pins_production_defaults(self):
        cfg = self._cfg("sampled", "off")
        self.assertEqual(cfg.whisper_mode, "sampled")
        self.assertFalse(cfg.line_order_audio_confirm)

    def test_escalation_follows_the_shipped_default(self):
        # Asserted against SETTINGS_SPEC, not a literal: the matrix used to pin this True, which
        # silently overrode a changed default and made a whole measurement read "no effect".
        from verifyarr.settings import SETTING_DEFS
        for key, attr in (("sync.escalate_sampled_to_full", "escalate_sampled_to_full"),
                          ("sync.escalate_min_bad_samples", "escalate_min_bad_samples"),
                          ("sync.anchor_suspect_min_samples", "anchor_suspect_min_samples"),
                          ("sync.min_change_seconds", "min_change_seconds"),
                          ("sync.sample_count", "sample_count"),
                          ("sync.clip_seconds", "clip_seconds")):
            self.assertEqual(getattr(self._cfg("sampled", "off"), attr), SETTING_DEFS[key][2],
                             f"{key} is pinned in cfg_for and no longer follows the default")

    def test_full_with_audio_confirm(self):
        cfg = self._cfg("full", "on")
        self.assertEqual(cfg.whisper_mode, "full")
        self.assertTrue(cfg.line_order_audio_confirm)


class IndexAnalysisTests(unittest.TestCase):
    def test_swap_index_detail_splits_injected_and_extra(self):
        d = M.swap_index_detail([3, 7], [3, 5, 7, 9])
        self.assertEqual(d["n_injected"], 2)
        self.assertEqual(d["n_injected_fixed"], 2)
        self.assertEqual(d["frac_injected_fixed"], 1.0)
        self.assertEqual(d["n_noninjected_fixed"], 2)
        self.assertEqual(d["noninjected_fixed"], [5, 9])

    def test_swap_index_detail_no_injected(self):
        d = M.swap_index_detail([], [5])
        self.assertIsNone(d["frac_injected_fixed"])
        self.assertEqual(d["n_noninjected_fixed"], 1)

    def test_agreement_unanimous_vs_singleton(self):
        a = M.agreement([{1, 2, 3}, {2, 3, 4}, {2, 5}])
        self.assertEqual(a["unanimous"], [2])
        self.assertEqual(a["n_union"], 5)
        self.assertEqual(a["jaccard"], round(1 / 5, 3))
        self.assertEqual(a["singleton"], [1, 4, 5])

    def test_agreement_empty(self):
        a = M.agreement([[], []])
        self.assertIsNone(a["jaccard"])
        self.assertEqual(a["n_union"], 0)


class DriftCaseTests(unittest.TestCase):
    def test_scores_recovery_skips_drift_slug_only(self):
        self.assertFalse(M.scores_recovery("C_S03E04", "clean"))
        self.assertFalse(M.scores_recovery("C_S03E04", "drift"))
        self.assertTrue(M.scores_recovery("SH_S01E01", "clean"))
        self.assertTrue(M.scores_recovery("SH_S01E01", "drift"))
        self.assertFalse(M.scores_recovery("SH_S01E01", "swap"))

    def test_summary_reports_drift_detection_not_recovery(self):
        rows = [
            {"status": "ok", "model": "turbo", "slug": "C_S03E04", "scenario": "clean",
             "mode": "full", "audio_confirm": "on", "drift_case": True,
             "flag": "SUSPECT", "sync": "resynced",
             "injected_p50": 0.0, "lo_fixed": 1, "lo_fixed_indices": [5],
             "lo_flagged": 0, "lo_flagged_indices": [], "escalated": False},
            {"status": "ok", "model": "turbo", "slug": "SH_S01E01", "scenario": "clean",
             "mode": "full", "audio_confirm": "on", "drift_case": False,
             "flag": "ok", "sync": "already in sync",
             "injected_p50": 0.0, "recovered": {"n": 5, "p50": 0.2, "frac_le_1_0s": 1.0},
             "untouched": True, "escalated": False,
             "lo_fixed": 0, "lo_fixed_indices": [], "lo_flagged": 0,
             "lo_flagged_indices": []},
        ]
        s = M.build_summary(rows, ["turbo"], ["C_S03E04", "SH_S01E01"],
                            ["clean"], ["full"], ["on"])
        # Recovery mean comes from the clean slug alone.
        self.assertEqual(s["by_scenario"]["clean"]["mean_p50"], 0.2)
        dc = s["drift_case"]
        self.assertEqual(dc["slugs"], ["C_S03E04"])
        self.assertEqual(dc["runs"], 1)
        self.assertEqual(dc["by_scenario"]["clean"]["suspect"], 1)
        self.assertEqual(dc["by_scenario"]["clean"]["sync_counts"], {"resynced": 1})


class ExtendedSummaryTests(unittest.TestCase):
    def _rows(self):
        return [
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "clean",
             "mode": "full", "audio_confirm": "on", "lo_fixed": 2, "lo_flagged": 1,
             "lo_fixed_indices": [3, 7], "lo_flagged_indices": [9],
             "untouched": False, "escalated": False,
             "recovered": {"n": 5, "p50": 0.2, "frac_le_1_0s": 1.0}},
            {"status": "ok", "model": "tiny", "slug": "A", "scenario": "clean",
             "mode": "full", "audio_confirm": "on", "lo_fixed": 3, "lo_flagged": 0,
             "lo_fixed_indices": [3, 5, 7], "lo_flagged_indices": [],
             "untouched": False, "escalated": False,
             "recovered": {"n": 5, "p50": 0.4, "frac_le_1_0s": 0.9}},
            {"status": "ok", "model": "turbo", "slug": "A", "scenario": "swap",
             "mode": "full", "audio_confirm": "on", "lo_fixed": 3,
             "lo_fixed_indices": [3, 5, 7], "escalated": False,
             "swap": {"n_swapped": 2, "n_restored": 1, "frac_restored": 0.5},
             "swap_detail": {"n_injected": 2, "n_injected_fixed": 1,
                             "frac_injected_fixed": 0.5, "n_noninjected_fixed": 2,
                             "noninjected_fixed": [5, 7]},
             "swap_noticed": True},
        ]

    def test_agreement_and_swap_detail(self):
        s = M.build_summary(self._rows(), ["turbo", "tiny"], ["A"],
                            ["clean", "swap"], ["full"], ["on"])
        agr = s["agreement_clean"]["A"]["full"]["on"]
        self.assertEqual(agr["fixed"]["unanimous"], [3, 7])
        self.assertEqual(agr["fixed"]["n_singleton"], 1)
        self.assertEqual(s["swap_detail"]["full.on"]["mean_frac_injected_fixed"], 0.5)
        self.assertEqual(s["swap_detail"]["full.on"]["mean_n_noninjected_fixed"], 2)
        # clean->swap delta is small here: the fixer mostly rewrites the same cues.
        self.assertEqual(s["clean_swap_delta"]["full.on"]["pairs"], 1)
        self.assertEqual(s["clean_swap_delta"]["full.on"]["mean_delta"], 1.0)
        self.assertEqual(s["escalation"]["full"]["escalated"], 0)


class AudioCacheTests(unittest.TestCase):
    def test_seed_points_at_existing_wav(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "C_S03E03.wav"
            wav.write_bytes(b"fake-wav")
            old = M.WAV_DIR
            M.WAV_DIR = Path(td)
            try:
                video = Path("/media/episode.mkv")
                self.assertEqual(M.audio_cache_for("C_S03E03", video), {video: wav})
            finally:
                M.WAV_DIR = old

    def test_missing_wav_falls_back_to_empty_cache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = M.WAV_DIR
            M.WAV_DIR = Path(td)
            try:
                self.assertEqual(M.audio_cache_for("NOPE", Path("/media/x.mkv")), {})
            finally:
                M.WAV_DIR = old

    def test_seeded_cache_resolves_without_extraction(self):
        from verifyarr import sync_engine
        video, wav = Path("/media/episode.mkv"), Path("/staging/EP.wav")
        cache = {video: wav}
        self.assertEqual(sync_engine.resolve_alass_reference(video, cache, Path("/tmp")), wav)
        self.assertEqual(cache, {video: wav})  # untouched, no ffmpeg side effect

    def test_run_one_passes_shared_cache_to_sync_pair(self):
        import tempfile
        from types import SimpleNamespace
        from verifyarr import db as _db, pipeline
        seen = []
        subs = _subs(["Hello world.", "Second line here."])
        real_sync, real_finish, real_save = (
            pipeline.sync_pair, pipeline.correctness_and_finish,
            _db.save_full_transcript_cache)
        pipeline.sync_pair = lambda *a, **k: (seen.append(a[4]) or ({"note": ""}, subs))
        pipeline.correctness_and_finish = lambda *a, **k: a[5]
        _db.save_full_transcript_cache = lambda *a, **k: None
        try:
            with tempfile.TemporaryDirectory() as td:
                # enable_correctness_check=False keeps pipeline.screen_pair (which run_one now
                # calls before sync_pair) out of the way: this test is about the audio cache
                # reaching sync_pair, not about screening.
                cfg = SimpleNamespace(stt_provider="groq", groq_model="turbo",
                                      use_local_whisper=True,
                                      local_whisper_model="/models/ggml-tiny.en.bin",
                                      enable_correctness_check=False, has_stt_configured=False,
                                      line_order_enabled=True, line_order_audio_confirm=True)
                shared = {Path("/media/v.mkv"): Path("/staging/V.wav")}
                M.run_one(Path(td), Path("/media/v.mkv"), subs, "en",
                          [{"start": 0.0, "end": 1.0, "text": "hi"}],
                          cfg, None, "tag", "full", shared)
                self.assertEqual(seen, [shared])
                self.assertIs(seen[0], shared)
        finally:
            pipeline.sync_pair = real_sync
            pipeline.correctness_and_finish = real_finish
            _db.save_full_transcript_cache = real_save


if __name__ == "__main__":
    unittest.main()
