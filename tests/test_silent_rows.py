"""Silent pass-throughs: files that were neither fixed nor warned about.

Each test runs the real pipeline (M.run_one, fresh DB) against matrix
fixtures with matrix-seeded corruption, so a dead path fails loudly.
The rows were chosen because they are stable on a fresh DB (verified), not only
in the matrix's accumulated shard state -- SH_S01E06 sampled is deliberately
left out: it depends on the cache (18 raw anchors fresh, 46 primed).

Needs the staging data (VERIFYARR_TEST_DATA) like the matrix; skipped elsewhere.
"""
from __future__ import annotations

import copy
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
from kg_env import MODEL, KgReplayCase
from verifyarr import db



def _run(slug, scenario, mode="sampled", audio="off", jitter_lo_hi=None):
    """Matrix-faithful single row: seeded corruption through M.run_one on a fresh DB."""
    return _runs(slug, scenario, mode, audio, jitter_lo_hi)[-1]


def _runs(slug, scenario, mode="sampled", audio="off", jitter_lo_hi=None, repeat=1):
    """[(row, rec)] for the same corrupted file run `repeat` times on one DB."""
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    lang, segments = M.audio_evidence(MODEL, slug, fx)
    assert segments, f"no sweep segments for {slug}"
    orig = M.subs_for(slug, fx)
    rng = random.Random(f"matrix-v1:{slug}:{scenario}")
    if jitter_lo_hi is not None:
        corrupted, _, _ = M.corrupt_jitter(copy.deepcopy(orig), rng, *jitter_lo_hi)
    else:
        corrupted, _, _ = M.SCENARIOS[scenario](copy.deepcopy(orig), rng)
    work = Path(tempfile.mkdtemp(prefix="silent_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, audio, groq_model=MODEL)
        object.__setattr__(cfg, "fps_check_enabled", True)
        cache = M.audio_cache_for(slug, video)
        out = []
        for _ in range(repeat):
            row, after = M.run_one(work, video, copy.deepcopy(corrupted), lang, segments, cfg,
                                   conn, "t", mode, cache)
            out.append((row, M.summarize(M.timing_errors(list(orig.events),
                                                         list(after.events), None))))
        return out
    finally:
        conn.close()


class SilentBlockTests(KgReplayCase):
    def test_resync_remainder_warns(self):
        """SH_S01E06 piecewise_c full: resync almost fixes it, but not quite --
        the remainder must warn, not slip through silently. The improvement is kept."""
        row, rec = _run("SH_S01E06", "piecewise_c", mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        self.assertIn("anchor region(s)", row.get("sync_status") or "",
                      "resync fixup must stay on disk; only the verdict changes")
        self.assertIn("REMAINS", row.get("note") or "")
        self.assertGreater(rec.get("frac_le_1_0s"), 0.85)

    def test_good_resync_keeps_its_fix_but_says_it_is_unverified(self):
        """SH_S01E01 full: resync reaches 0.964 -- that fix must STAY on disk.

        The verdict warns anyway, and that is a deliberate policy shift: block errors
        only have to be DETECTED (the user just fetches a new subtitle), and a
        half-finished repair cannot be told apart from a whole one on the corrected file
        -- 0 of 22 half-repaired rows still step at the sampled points.
        The price of catching those 18 is that 12 well-working block fixes also
        warn. A warning on a fixed file is noise; a silent half-fixed file is
        dangerous. Healthy files are untouched: 0 flag changes on clean/p03/dropdup/
        missing_middle over 1656 rows.
        """
        row, rec = _run("SH_S01E01", "piecewise", mode="full")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                "the fix itself must survive -- only the verdict changes")
        self.assertIn("anchor region(s)", row.get("sync_status") or "")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")
        # Either wording is the same policy: this file steps at 2587s/2653s by ~24s
        # after the resync, so here it is the evidenced branch rather than the
        # carried-forward one. 96% of cues are still fine -- the damage sits late.
        note = row.get("note") or ""
        self.assertTrue("Block boundary REMAINS" in note
                        or "NOT verified across the whole episode" in note, note[-200:])

    def test_block_repair_edges_warn_full(self):
        """SH_S01E01 block_rand0 full: alass' 2-block fit also moved the 13
        correct lines before the block by 19.6 s. The edges cannot be verified --
        warn, keep the fix."""
        row, rec = _run("SH_S01E01", "block_rand0", mode="full")
        self.assertIn("sync block(s)", row.get("sync_status") or "")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent (note: {(row.get('note') or '')[-200:]})")
        self.assertGreater(rec.get("frac_le_1_0s"), 0.9)

    def test_block_repair_edges_warn_sampled(self):
        """KG_PB_S05E01 block_rand2 sampled: 3 sync blocks, 6 lines left."""
        row, rec = _run("KG_BIL_S01E01", "block_rand2", mode="sampled")
        self.assertIn("sync block(s)", row.get("sync_status") or "")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")

    def test_rerun_on_cache_keeps_missing_middle(self):
        """The same file twice on the same DB: a cache hit must not lose the arm 2 check."""
        rows = _runs("SH_S01E01", "missing_middle", mode="full", audio="off", repeat=2)
        self.assertEqual([r.get("correctness_flag") for r, _ in rows], ["SUSPECT", "SUSPECT"])

    def test_escalated_block_file_is_judged_on_the_full_transcript(self):
        """KG_BB_S01E01 sampled piecewise_b: alass' 3-block fit escalates, but resync and
        recheck fell back to 16 sampled clips, and the half-repaired file
        went through silently (0.440, ok). The verdict must be taken on the purchased transcript."""
        row, rec = _run("KG_EUP_S01E01", "piecewise_b", mode="sampled")
        self.assertLess(rec.get("frac_le_1_0s"), 0.90)
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_lone_huge_anchor_without_block_fit_escalates(self):
        """KG_BMS_S01E01 sampled piecewise_c: alass saw one offset, but one anchor was
        +20 s off. Without a multi-block fit it never escalated and went through silently (0.425)."""
        row, rec = _run("KG_BMS_S01E01", "piecewise_c", mode="sampled")
        # Escalated, the anchor resync can now fix it (0.903); either outcome is fine.
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_screen_does_not_vouch_for_a_subtitle_that_ends_early(self):
        """KG_EUP_S01E01 sampled cut_version: 300s cut out, everything after sits 300s early.
        The screen's 5 clips all fell before 405s and agreed, so alass never ran and
        the file went through silently (0.560). The subtitle ends ~300s before the audio."""
        row, rec = _run("KG_EUP_S01E01", "cut_version", mode="sampled")
        self.assertNotIn("alass was not run", row.get("note") or "")
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_lone_huge_anchor_warns(self):
        """SH_S01E01 sampled: alass' 4-block fit is 25 s wrong in one block, but
        sparse sampling gives only ONE witness -- min_samples suppresses it,
        so the file went through silently (0.855). Now it escalates, and the resync on
        the full transcript fixes it (0.964). Silent is the one forbidden outcome."""
        row, rec = _run("SH_S01E01", "piecewise", mode="sampled")
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_per_cue_jitter_warns(self):
        """SH_S01E01 full jitter +/-0.5-1.5s: averages away in every anchor's
        shift, so only the spread inside each anchor shows it (0.53s)."""
        row, rec = _run("SH_S01E01", "jitter", mode="full",
                        jitter_lo_hi=(0.5, 1.5))
        self.assertLess(rec.get("frac_le_1_0s"), 0.90)
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")
        self.assertIn("Cue timing is noisy", row.get("note") or "")

    def test_jitter_rule_spares_the_noisiest_healthy_file(self):
        """SH_S01E01 sampled uniform_neg: fixed, and the highest anchor spread of
        any healthy row (0.445s). The jitter rule must not flag it."""
        row, _ = _run("SH_S01E01", "uniform_neg", mode="sampled")
        self.assertTrue((row.get("sync_status") or "").startswith("fixed"))
        self.assertEqual(row.get("correctness_flag"), "ok", (row.get("note") or "")[-300:])


class MissingMiddleTests(KgReplayCase):
    def test_missing_middle_full_warns_without_rewriting(self):
        """SH_S01E01 full missing_middle: 300s of cues gone, survivors correct.
        Detection only: SUSPECT + untouched file, timings unchanged."""
        row, _ = _run("SH_S01E01", "missing_middle", mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        note = row.get("note") or ""
        self.assertIn("part of the episode is missing", note)
        self.assertRegex(note, r"no lines for \d+ s at \d+:\d\d-\d+:\d\d")
        self.assertTrue((row.get("sync_status") or "").startswith(
            ("already in sync", "left unchanged")),
            f"must not rewrite (sync: {row.get('sync_status')})")
        self.assertIn(row.get("line_order_fixed"), (None, 0))

    def test_missing_middle_sampled_warns_without_rewriting(self):
        """Same hole in sampled mode: the 300s cue gap alone buys the full
        transcript, and the verdict is taken on that -- not on the 5 clips."""
        row, _ = _run("SH_S01E01", "missing_middle", mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        self.assertIn("part of the episode is missing", row.get("note") or "")
        self.assertTrue((row.get("sync_status") or "").startswith(
            ("already in sync", "left unchanged")),
            f"must not rewrite (sync: {row.get('sync_status')})")
        self.assertIn(row.get("line_order_fixed"), (None, 0))

    def test_big_healthy_gap_sampled_stays_ok(self):
        """KG_BIL_S01E01 clean sampled: natural 174s gap (1652-1826, credits music,
        ~10 words). The gap trigger may buy the full transcript, but the file
        must stay ok and untouched."""
        row, rec = _run("KG_BIL_S01E01", "clean", mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "ok", (row.get("note") or "")[-300:])
        self.assertTrue((row.get("sync_status") or "").startswith(
            ("already in sync", "left unchanged")),
            f"rewrote a healthy file (sync: {row.get('sync_status')})")
        self.assertEqual(rec.get("frac_le_1_0s"), 1.0)


class StretchNoteTests(KgReplayCase):
    def test_presync_note_survives_deferral(self):
        """SH_S01E06 full drift: presync fires, but alass goes multi-block and
        the defer path lost the presync text -- the note lied "alass only". The fix
        must be credited, and late cues must be within 1 s."""
        row, rec = _run("SH_S01E06", "drift", mode="full")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "",
                      f"presync fired invisibly (note: {(row.get('note') or '')[:300]})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90)

    def test_sampled_stretch_presyncs(self):
        """KG_BIL_S01E01 sampled drift: the keep gate blocked (0.879) before
        the count trim -- locks in that a sampled stretch fires through
        the pipeline, not only in unit tests."""
        row, rec = _run("KG_BB_S01E01", "drift", mode="sampled")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "",
                      f"presync never fired (note: {(row.get('note') or '')[:300]})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90)


if __name__ == "__main__":
    unittest.main()
