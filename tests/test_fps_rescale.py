"""Framerate (1001/1000) tilt detection: end-to-end through process_pair.

Regression test for the round-1 failure mode, where the detector was validated
through scratchpad scripts calling the functions directly while the pipeline
path was dead (samples carried no raw anchor points): 0 firings in 360 matrix
rows. These tests run real files through pipeline.sync_pair +
correctness_and_finish with matrix fixtures, so a dead path fails loudly.

Needs the staging tree (/mnt/c/...) like the matrix itself; skipped elsewhere.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
from kg_env import MODEL, KgReplayCase
from verifyarr import db
from verifyarr.subtitles import load_subs

MODEL = "tiny.en-greedy-cpu"  # shipped model: the only one that matters here




class FpsRescaleIntegrationTests(KgReplayCase):
    """Real drift must be fixed, healthy files untouched, alass fixes unmolested."""

    def _run(self, slug, subs, mode="sampled", audio="off"):
        fx = M.fixture(slug)
        video = M.media_dir(slug) / fx["video_name"]
        lang, segments = M.audio_evidence(MODEL, slug, fx)
        self.assertTrue(segments, f"no sweep segments for {slug}")
        work = Path(tempfile.mkdtemp(prefix="fps_test_"))
        conn = db.connect(work / "t.db")
        try:
            cfg = M.cfg_for(conn, mode, audio, groq_model=MODEL)
            object.__setattr__(cfg, "fps_check_enabled", True)
            cache = M.audio_cache_for(slug, video)
            tag = f"{slug}.{mode}.{audio}"
            row, _ = M.run_one(work, video, subs, lang, segments, cfg, conn,
                               tag, mode, cache)
            out = load_subs(work / f"{tag}.srt")
            return row, out
        finally:
            conn.close()

    def _late_cue(self, subs, after_s=1000.0):
        cands = [e for e in subs.events if e.start / 1000.0 > after_s]
        self.assertTrue(cands, "no late cue found")
        return cands[-1]

    def _run_injected(self, slug, scenario, mode="sampled"):
        """The matrix's own seeded corruption of the verified subtitle, through the whole pipeline."""
        import copy
        import random
        orig = M.subs_for(slug, M.fixture(slug))
        bad, _, _ = M.SCENARIOS[scenario](copy.deepcopy(orig), random.Random(f"matrix-v1:{slug}:{scenario}"))
        row, out = self._run(slug, bad, mode)
        return row, out, orig

    def test_real_drift_gets_fixed_sampled(self):
        """KG_BOYS_S01E01 with a 24 fps subtitle on 23.976 audio (the matrix's fps_late) is rescaled."""
        row, out, orig = self._run_injected("KG_BOYS_S01E01", "fps_late")
        self.assertIsNotNone(row.get("fps_ratio"),
                             f"fps never fired (note: {(row.get('note') or '')[:200]})")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0, delta=1.0,
                               msg="late cue not brought back to its original time")

    def test_real_drift_gets_fixed_full(self):
        row, _out, _orig = self._run_injected("KG_BOYS_S01E01", "fps_late", mode="full")
        self.assertIsNotNone(row.get("fps_ratio"),
                             f"fps never fired in full mode (note: {(row.get('note') or '')[:200]})")

    def test_healthy_file_untouched(self):
        """SH_S01E01 (facit-egnet): no fps bookkeeping, still already in sync."""
        orig = M.subs_for("SH_S01E01", M.fixture("SH_S01E01"))
        row, _ = self._run("SH_S01E01", orig)
        self.assertIsNone(row.get("fps_ratio"), f"fps fired on healthy file: {row.get('note')}")
        self.assertEqual(row.get("sync_status"), "already in sync")

    def test_injected_scale_not_double_fixed(self):
        """Injected x1001/1000 is fixed by alass itself; fps must stay silent after it."""
        import copy
        orig = M.subs_for("SH_S01E01", M.fixture("SH_S01E01"))
        bad = copy.deepcopy(orig)
        for e in bad.events:
            e.start = int(round(e.start * 1001 / 1000.0))
            e.end = int(round(e.end * 1001 / 1000.0))
        row, out = self._run("SH_S01E01", bad)
        self.assertIsNone(row.get("fps_ratio"),
                          f"fps fought alass' own fix: {row.get('note')}")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0, delta=1.0,
                               msg="alass did not repair the injected scale")


class StretchProbeUnitTests(KgReplayCase):
    """The four readings, on pools whose shape is known by construction."""

    @staticmethod
    def _pool(fn, n=45, step=30.0, noise=0.2, seed=7):
        import random
        rng = random.Random(seed)
        return [(i * step, fn(i * step) + rng.gauss(0, noise)) for i in range(n)]

    def test_stretch_reads_its_own_rate(self):
        from verifyarr.subtitles import stretch_probe
        p = stretch_probe(self._pool(lambda t: -0.02 * t))
        self.assertAlmostEqual(p["slope"], -0.02, delta=0.002)
        self.assertGreater(abs(p["rho"]), 0.95)
        self.assertGreater(p["keep_frac"], 0.9)
        self.assertLess(p["resid"], 0.6)

    def test_blocks_leave_most_of_the_pool_off_the_line(self):
        """The discriminator: a line cannot cover several blocks at once."""
        from verifyarr.subtitles import stretch_probe
        shifts = [-12.0, 7.0, -5.0, 14.0, -9.0, 3.0]
        p = stretch_probe(self._pool(lambda t: shifts[min(5, int(t // 240))]))
        self.assertTrue(p is None or p["keep_frac"] < 0.65,
                        f"block pool looked like one line: {p}")

    def test_single_monotone_step_is_not_a_rate(self):
        """One big step ranks monotone (rho 0.87 on a clean step) -- rho alone would
        take it. keep_frac and the flattening test are what stop it."""
        from verifyarr.subtitles import stretch_probe, STRETCH_MIN_KEEP_FRAC
        p = stretch_probe(self._pool(lambda t: 0.0 if t < 660 else 12.0))
        self.assertTrue(p is None or p["keep_frac"] < STRETCH_MIN_KEEP_FRAC
                        or p["resid"] > 0.6, f"step read as a rate: {p}")

    def test_healthy_pool_has_no_tilt_and_no_gain(self):
        from verifyarr.subtitles import stretch_probe
        p = stretch_probe(self._pool(lambda t: 0.15, noise=0.3))
        self.assertLess(abs(p["tilt"]), 1.0)
        self.assertLess(p["gain"], 0.5)

    def test_line_trim_survives_a_slope_the_median_trim_would_discard(self):
        """Why the fit trims about the LINE: a 2% pool spans +-25s, so a median trim
        throws the signal away. robust_rate_fit must keep essentially everything."""
        from verifyarr.subtitles import robust_rate_fit, anchor_drift_signature
        pool = self._pool(lambda t: -0.02 * t)
        fit = robust_rate_fit(pool)
        self.assertGreater(fit["n"] / fit["n_raw"], 0.9)
        sig = anchor_drift_signature(pool)
        self.assertLess(sig["n"], 0.7 * fit["n"],
                        "median trim should discard much of a 2% pool; the line trim should not")

    def test_bad_fit_returns_none_rather_than_the_untrimmed_pool(self):
        """Regression: returning pts on a failed trim reported resid 3.76s as a
        keep_frac of 1.00 -- a tight fit over a mess."""
        from verifyarr.subtitles import robust_rate_fit
        import random
        rng = random.Random(3)
        scattered = [(i * 30.0, rng.uniform(-40, 40)) for i in range(45)]
        fit = robust_rate_fit(scattered, floor=20)
        self.assertTrue(fit is None or fit["n"] < 45)

    def test_spearman_is_signed_and_tie_safe(self):
        from verifyarr.subtitles import spearman_rho
        self.assertAlmostEqual(spearman_rho([(i, i) for i in range(10)]), 1.0)
        self.assertAlmostEqual(spearman_rho([(i, -i) for i in range(10)]), -1.0)
        self.assertIsNone(spearman_rho([(i, 5.0) for i in range(10)]))
        self.assertIsNone(spearman_rho([(0, 0), (1, 1)]))


class StretchRescaleIntegrationTests(KgReplayCase):
    """The round-1 lesson: measure through process_pair, never through the functions
    alone. A dead path passed seven unit tests and fired 0 times in 360 matrix rows."""

    _run = FpsRescaleIntegrationTests._run
    _late_cue = FpsRescaleIntegrationTests._late_cue
    _run_injected = FpsRescaleIntegrationTests._run_injected

    def test_two_percent_stretch_is_measured_and_undone(self):
        """SH_S01E01 + 2%: presync or post-alass stretch must undo the rate.

        Either correction path counts: the pre-alass presync (a "Pre-sync before
        alass: rate" note, no fps_ratio -- the file never reaches the post-alass
        branch) or the post-alass stretch rescale (fps_ratio). The late-cue timing
        below is the real proof; the path is implementation detail."""
        import copy
        orig = M.subs_for("SH_S01E01", M.fixture("SH_S01E01"))
        bad = copy.deepcopy(orig)
        for e in bad.events:
            e.start, e.end = int(e.start * 1.02), int(e.end * 1.02)
        row, out = self._run("SH_S01E01", bad)
        fixed_by_post_alass = (row.get("fps_ratio") or "").startswith("stretch")
        fixed_by_presync = "Pre-sync before alass: rate" in (row.get("note") or "")
        self.assertTrue(fixed_by_post_alass or fixed_by_presync,
                        f"stretch never fired (note: {(row.get('note') or '')[:300]})")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0, delta=1.0,
                               msg="late cue not brought back to its original time")

    def test_two_percent_stretch_fixed_in_full_mode(self):
        """Full mode owns the whole transcript, so it must fix a stretch at least
        as reliably as sampled -- not worse. Fresh DB, no cache warmup."""
        import copy
        orig = M.subs_for("SH_S01E06", M.fixture("SH_S01E06"))
        bad = copy.deepcopy(orig)
        for e in bad.events:
            e.start, e.end = int(e.start * 1.02), int(e.end * 1.02)
        row, out = self._run("SH_S01E06", bad, mode="full")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "",
                      f"presync never fired in full mode (note: {(row.get('note') or '')[:300]})")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0, delta=1.0,
                               msg="late cue not brought back to its original time")

    def test_block_errors_do_not_trigger_the_stretch_branch(self):
        import random
        orig = M.subs_for("SH_S01E01", M.fixture("SH_S01E01"))
        for seed in (0, 1, 2):
            bad, _k, _d = M.corrupt_piecewise(orig, random.Random(f"SH_S01E01|{seed}"))
            row, _ = self._run("SH_S01E01", bad)
            self.assertFalse((row.get("fps_ratio") or "").startswith("stretch"),
                             f"stretch fired on piecewise seed {seed}: {row.get('note')}")

    def test_healthy_file_still_untouched_by_the_new_branch(self):
        orig = M.subs_for("SH_S01E01", M.fixture("SH_S01E01"))
        row, _ = self._run("SH_S01E01", orig)
        self.assertIsNone(row.get("fps_ratio"), f"stretch fired on healthy: {row.get('note')}")

    def test_zero_point_one_percent_still_takes_the_discrete_path(self):
        """The 8s tilt floor is the branch selector: 0.1% must not be read as a rate."""
        for scenario, name in (("fps_late", "24/23.976"), ("fps_early", "23.976/24")):
            row, _out, _orig = self._run_injected("KG_BOYS_S01E01", scenario)
            self.assertEqual(row.get("fps_ratio"), name,
                             f"discrete path lost the {scenario} case: {row.get('note')}")


if __name__ == "__main__":
    unittest.main()
