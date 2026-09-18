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
from verifyarr import db
from verifyarr.subtitles import load_subs

MODEL = "tiny.en-greedy-cpu"  # shipped model: the only one that matters here

STAGING_OK = (
    M.SWEEP.exists()
    and (M.SWEEP / MODEL / "C_S03E04.json").exists()
    and (M.SWEEP / MODEL / "C_S03E03.json").exists()
)


def _needs_staging():
    return unittest.skipUnless(STAGING_OK, "needs whisper_gpu_staging sweep data")


class FpsRescaleIntegrationTests(unittest.TestCase):
    """Real drift must be fixed, healthy files untouched, alass fixes unmolested."""

    def _run(self, slug, subs, mode="sampled", audio="on"):
        fx = M.fixture(slug)
        video = M.media_dir(slug) / fx["video_name"]
        if not video.exists():
            self.skipTest(f"no video for {slug}")
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

    @_needs_staging()
    def test_real_drift_gets_fixed_sampled(self):
        """C_S03E04 (DTW-confirmed 24->23.976 drift) must be rescaled, right direction."""
        orig = M.subs_for("C_S03E04", M.fixture("C_S03E04"))
        row, out = self._run("C_S03E04", orig)
        self.assertEqual(row.get("fps_ratio"), "24 -> 23.976",
                         f"fps never fired (note: {(row.get('note') or '')[:200]})")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0 / (1001 / 1000.0),
                               delta=0.2, msg="late cue did not move to /1.001")
        self.assertGreater(abs(n.start - o.start) / 1000.0, 0.5,
                           "file effectively unchanged")

    @_needs_staging()
    def test_real_drift_gets_fixed_full(self):
        orig = M.subs_for("C_S03E04", M.fixture("C_S03E04"))
        row, out = self._run("C_S03E04", orig, mode="full")
        self.assertEqual(row.get("fps_ratio"), "24 -> 23.976",
                         f"fps never fired in full mode (note: {(row.get('note') or '')[:200]})")

    @_needs_staging()
    def test_healthy_file_untouched(self):
        """C_S03E03 (facit-egnet): no fps bookkeeping, still already in sync."""
        orig = M.subs_for("C_S03E03", M.fixture("C_S03E03"))
        row, _ = self._run("C_S03E03", orig)
        self.assertIsNone(row.get("fps_ratio"), f"fps fired on healthy file: {row.get('note')}")
        self.assertEqual(row.get("sync_status"), "already in sync")

    @_needs_staging()
    def test_injected_scale_not_double_fixed(self):
        """Injected x1001/1000 is fixed by alass itself; fps must stay silent after it."""
        import copy
        orig = M.subs_for("C_S03E03", M.fixture("C_S03E03"))
        bad = copy.deepcopy(orig)
        for e in bad.events:
            e.start = int(round(e.start * 1001 / 1000.0))
            e.end = int(round(e.end * 1001 / 1000.0))
        row, out = self._run("C_S03E03", bad)
        self.assertIsNone(row.get("fps_ratio"),
                          f"fps fought alass' own fix: {row.get('note')}")
        o, n = self._late_cue(orig), self._late_cue(out)
        self.assertAlmostEqual(n.start / 1000.0, o.start / 1000.0, delta=1.0,
                               msg="alass did not repair the injected scale")


if __name__ == "__main__":
    unittest.main()
