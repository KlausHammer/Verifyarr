"""Stille gennemloeb: filer der hverken blev rettet eller advaret om.

Hver test koerer den rigtige roerledning (M.run_one, frisk DB) mod matrix-
fixtures med matrix-seedet korruption, saa en doed sti fejler hoejlydt.
Raekkerne er valgt fordi de er stabile paa frisk DB (verificeret), ikke kun
i matricens akkumulerede shard-tilstand -- SH_S01E06 sampled er bevidst
udeladt: den er cache-afhaengig (18 raa ankre frisk, 46 primet).

Needs the staging tree (/mnt/c/...) like the matrix; skipped elsewhere.
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
from verifyarr import db

MODEL = "tiny.en-greedy-cpu"
STAGING_OK = (
    M.SWEEP.exists()
    and (M.SWEEP / MODEL / "C_S03E04.json").exists()
    and (M.SWEEP / MODEL / "C_S03E03.json").exists()
)
_needs_staging = unittest.skipUnless(STAGING_OK, "needs whisper_gpu_staging sweep data")


def _run(slug, scenario, mode="sampled", audio="on", jitter_lo_hi=None):
    """Matrix-faithful single row: seeded corruption through M.run_one on a fresh DB."""
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    if not video.exists():
        raise unittest.SkipTest(f"no video for {slug}")
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
        row, after = M.run_one(work, video, corrupted, lang, segments, cfg,
                               conn, "t", mode, cache)
        rec = M.summarize(M.timing_errors(list(orig.events), list(after.events), None))
        return row, rec
    finally:
        conn.close()


@_needs_staging
class SilentBlockTests(unittest.TestCase):
    def test_resync_remainder_warns(self):
        """SH_S01E06 piecewise_c full: resync fikser naesten men ikke helt --
        resten skal advare, ikke glide stille igennem. Forbedringen beholdes."""
        row, rec = _run("SH_S01E06", "piecewise_c", mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        self.assertIn("anchor region(s)", row.get("sync_status") or "",
                      "resync fixup must stay on disk; only the verdict changes")
        self.assertIn("REMAINS", row.get("note") or "")
        self.assertGreater(rec.get("frac_le_1_0s"), 0.85)

    def test_good_resync_keeps_its_fix_but_says_it_is_unverified(self):
        """SH_S01E01 full: resync naar 0.964 -- den rettelse skal BLIVE paa disken.

        Verdicten advarer alligevel, og det er et bevidst policy-skifte: blokfejl
        skal kun DETEKTERES (brugeren henter bare en ny undertekst), og en
        halvfaerdig reparation kan ikke skelnes fra en hel paa den korrigerede fil
        -- 0 af 22 halvt reparerede raekker stepper stadig ved de samplede punkter.
        Prisen for at fange de 18 er at 12 velfungerende blok-rettelser ogsaa
        advarer. En advarsel paa en rettet fil er stoej; en tavs halvrettet fil er
        farlig. Raske filer er uberoerte: 0 flagaendringer paa clean/p03/dropdup/
        missing_middle over 1656 raekker.
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
        """SH_S01E01 block_rand0 full: alass' 2-blok-fit flyttede ogsaa de 13
        korrekte linjer foer blokken 19,6 s. Kanterne kan ikke verificeres --
        advar, behold rettelsen."""
        row, rec = _run("SH_S01E01", "block_rand0", mode="full")
        self.assertIn("sync block(s)", row.get("sync_status") or "")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent (note: {(row.get('note') or '')[-200:]})")
        self.assertGreater(rec.get("frac_le_1_0s"), 0.9)

    def test_block_repair_edges_warn_sampled(self):
        """SH_S01E04 block_rand2 sampled: 3 sync-blokke, 6 linjer tilbage."""
        row, rec = _run("SH_S01E04", "block_rand2", mode="sampled")
        self.assertIn("sync block(s)", row.get("sync_status") or "")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")

    def test_escalated_block_file_is_judged_on_the_full_transcript(self):
        """C_S02E12 sampled piecewise_b: alass' 3-blok-fit eskalerer, men resync og
        recheck faldt tilbage til 16 samplede klip, og den halvt reparerede fil
        gik stille igennem (0.440, ok). Dommen skal tages paa det koebte transskript."""
        row, rec = _run("C_S02E12", "piecewise_b", mode="sampled")
        self.assertLess(rec.get("frac_le_1_0s"), 0.90)
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_lone_huge_anchor_without_block_fit_escalates(self):
        """C_S03E09 sampled piecewise_c: alass saa een offset, men eet anker stod
        +20s ude. Uden flerbloksfit eskalerede den aldrig og gik stille (0.425)."""
        row, rec = _run("C_S03E09", "piecewise_c", mode="sampled")
        # Escalated, the anchor resync can now fix it (0.903); either outcome is fine.
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_screen_does_not_vouch_for_a_subtitle_that_ends_early(self):
        """C_S03E08 sampled cut_version: 300s cut out, everything after sits 300s early.
        The screen's 5 clips all fell before 405s and agreed, so alass never ran and
        the file went through silently (0.560). The subtitle ends ~300s before the audio."""
        row, rec = _run("C_S03E08", "cut_version", mode="sampled")
        self.assertNotIn("alass was not run", row.get("note") or "")
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"silent again (note: {(row.get('note') or '')[:300]})")

    def test_lone_huge_anchor_warns(self):
        """SH_S01E01 sampled: alass' 4-blok-fit er 25s galt i een blok, men
        sparsom sampling giver kun EET vidne -- min_samples undertrykker det,
        saa filen gik stille igennem (0.855). Nu eskalerer den, og resyncen paa
        det fulde transskript retter den (0.964). Stille er det eneste forbudte."""
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


@_needs_staging
class MissingMiddleTests(unittest.TestCase):
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
        """SH_S01E02 clean sampled: natural 174s gap (1652-1826, credits music,
        ~10 words). The gap trigger may buy the full transcript, but the file
        must stay ok and untouched."""
        row, rec = _run("SH_S01E02", "clean", mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "ok", (row.get("note") or "")[-300:])
        self.assertTrue((row.get("sync_status") or "").startswith(
            ("already in sync", "left unchanged")),
            f"rewrote a healthy file (sync: {row.get('sync_status')})")
        self.assertEqual(rec.get("frac_le_1_0s"), 1.0)


@_needs_staging
class StretchNoteTests(unittest.TestCase):
    def test_presync_note_survives_deferral(self):
        """SH_S01E06 full drift: presync fyrer, men alass gaar multiblok og
        defer-stien tabte presync-teksten -- noten loej alass-only. Rettelsen
        skal tilskrives, og sen cue skal vaere inden for 1s."""
        row, rec = _run("SH_S01E06", "drift", mode="full")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "",
                      f"presync fired invisibly (note: {(row.get('note') or '')[:300]})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90)

    def test_sampled_stretch_presyncs(self):
        """SH_S01E02 sampled drift: keep-porten blokerede (0.879) foer
        count-trimmen -- laaser at sampled-str straekning fyrer gennem
        roerledningen, ikke kun i enhedstests."""
        row, rec = _run("SH_S01E02", "drift", mode="sampled")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "",
                      f"presync never fired (note: {(row.get('note') or '')[:300]})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90)


if __name__ == "__main__":
    unittest.main()
