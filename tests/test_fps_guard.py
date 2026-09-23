"""Post-rate guard: a rate/fps correction must leave the file quiet everywhere.

Regression tests for the six silent arm-1 rows (carry reference): sampled files
where a rate correction was applied on top of an already-fixed (or block-broken)
file and reported "fixed" with flag ok while the file stayed wrong.

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


def _run(slug, scenario, mode="sampled", audio="on"):
    """Matrix-faithful single row: seeded corruption through M.run_one on a fresh DB."""
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    if not video.exists():
        raise unittest.SkipTest(f"no video for {slug}")
    lang, segments = M.audio_evidence(MODEL, slug, fx)
    assert segments, f"no sweep segments for {slug}"
    orig = M.subs_for(slug, fx)
    corrupted, kept, _ = M.SCENARIOS[scenario](
        copy.deepcopy(orig), random.Random(f"matrix-v1:{slug}:{scenario}"))
    work = Path(tempfile.mkdtemp(prefix="fpsguard_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, audio, groq_model=MODEL)
        object.__setattr__(cfg, "fps_check_enabled", True)
        cache = M.audio_cache_for(slug, video)
        row, after = M.run_one(work, video, corrupted, lang, segments, cfg,
                               conn, "t", mode, cache)
        rec = M.summarize(M.timing_errors(list(orig.events), list(after.events),
                                           kept if kept else None))
        return row, rec
    finally:
        conn.close()


@_needs_staging
class FpsGuardTests(unittest.TestCase):
    def test_pure_shift_gets_no_stretch(self):
        """C_S02E12 uniform_neg sampled: pure -45s shift, no rate at all.

        Before: post-alass stretch -4.01% applied to alass' broken 2-block fit,
        rec 0.847 with flag ok (silent). The presync offset was already right;
        the rate must be rejected and the broken file warned about, not fixed."""
        row, rec = _run("C_S02E12", "uniform_neg")
        self.assertNotIn("stretch", (row.get("sync_status") or "") + (row.get("note") or ""),
                         f"rate applied to a pure shift (rec={rec.get('frac_le_1_0s')})")
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"still silent: rec={rec.get('frac_le_1_0s')} flag={row.get('correctness_flag')}")

    def test_pal_gets_no_ntsc(self):
        """SH_S01E05 pal_early sampled: 4.17% error, presync already fixed it.

        Before: fps 24->23.976 (0.1%) applied on top of presync's +4.10%,
        rec 0.316 with flag ok (silent). Rejecting the 0.1% leaves rec 1.0."""
        row, rec = _run("SH_S01E05", "pal_early")
        self.assertNotIn("framerate", row.get("sync_status") or "",
                         f"NTSC applied on top of a PAL fix (rec={rec.get('frac_le_1_0s')})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"presync fix lost: rec={rec.get('frac_le_1_0s')}")

    def test_fixed_shift_gets_no_fps(self):
        """SH_S01E06 uniform_m5 sampled: pure -5s shift, alass fixed it.

        Before: fps 24->23.976 applied to the corrected file (anchor tilt just
        over the floor), rec 0.396 with flag ok (silent)."""
        row, rec = _run("SH_S01E06", "uniform_m5")
        self.assertNotIn("framerate", row.get("sync_status") or "",
                         f"fps applied to a fixed file (rec={rec.get('frac_le_1_0s')})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"alass fix broken: rec={rec.get('frac_le_1_0s')}")


class QuartileResidualUnitTests(unittest.TestCase):
    """The guard reading, on pools whose shape is known by construction."""

    @staticmethod
    def _pool(fn, n=60, span=2400.0, noise=0.25, seed=11):
        import random
        rng = random.Random(seed)
        return [(i * span / n, i * span / n + fn(i * span / n) + rng.gauss(0, noise))
                for i in range(n)]

    def test_true_rate_leaves_every_quarter_quiet(self):
        from verifyarr.subtitles import max_quartile_residual_after
        pool = self._pool(lambda t: 0.02 * t)  # subtitle 2% late, like drift
        ratio = 1.0 / 1.02
        self.assertLess(max_quartile_residual_after(pool, ratio, 0.0), 1.0)

    def test_rate_on_flat_file_ramps_the_quarters(self):
        from verifyarr.subtitles import max_quartile_residual_after
        pool = self._pool(lambda t: 0.0)  # healthy flat file
        self.assertGreater(
            max_quartile_residual_after(pool, 1000 / 1001, 0.0), 1.0)

    def test_step_tail_survives_the_line_fit(self):
        from verifyarr.subtitles import max_quartile_residual_after
        pool = self._pool(lambda t: 0.04 * t if t < 1800 else 67.0)
        self.assertGreater(
            max_quartile_residual_after(pool, 1.0 / 1.04, 0.0), 1.0)


if __name__ == "__main__":
    unittest.main()
