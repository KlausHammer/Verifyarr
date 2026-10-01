"""SH_S01E04 full must not be touched: dense evidence needs 3 witnesses.

The user LOOKED at 22:47-23:47: the subtitle is correct, but full mode
rewrote the file on exactly 2 bad anchors out of ~51 (4 %). The sampled bar
(2) is calibrated on ~5-16 clips; on dense full evidence 3 applies.
C_S03E11 (a genuine block, 15+ witnesses) must still be caught.

Runs the real pipeline (M.run_one, fresh DB) on the GENUINE
subtitles + transcripts like genuine.py. Needs the staging data
(VERIFYARR_TEST_DATA); skipped elsewhere.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
from verifyarr import db

OUT = M.SWEEP.parent / "out"
STAGING_OK = OUT.exists() and (OUT / "SH_S01E04.json").exists()
_needs_staging = unittest.skipUnless(STAGING_OK, "needs whisper_gpu_staging out/ transcripts")


def _run_genuine(slug, mode="full"):
    """Genuine subtitle + genuine transcript through the pipeline, fresh DB."""
    fx = M.fixture(slug)
    subs = M.subs_for(slug, fx)
    video = M.media_dir(slug) / fx["video_name"]
    if not video.exists():
        raise unittest.SkipTest(f"no video for {slug}")
    op = OUT / f"{slug}.json"
    if not op.exists():
        raise unittest.SkipTest(f"no out transcript for {slug}")
    doc = json.loads(op.read_bytes().decode("utf-8", errors="replace"))
    segments, lang = M.sweep_segments(doc), M.sweep_language(doc)
    assert segments, f"no segments for {slug}"
    work = Path(tempfile.mkdtemp(prefix="sh04_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, "on", groq_model="genuine")
        cache = M.audio_cache_for(slug, video)
        row, _after = M.run_one(work, video, subs, lang, segments, cfg,
                                conn, f"sh04.{slug}.{mode}", mode, cache)
        return row
    finally:
        conn.close()


@_needs_staging
class Sh04FullTests(unittest.TestCase):
    def test_sh_s01e04_full_untouched_and_unflagged(self):
        """Confirmed false positive: a correct file rewritten on 2/51 anchors."""
        row = _run_genuine("SH_S01E04", "full")
        sync = row.get("sync_status") or ""
        self.assertNotIn("anchor region(s)", sync,
                         f"false rewrite (note: {(row.get('note') or '')[:300]})")
        self.assertTrue(sync.startswith("already in sync")
                        or sync.startswith("left unchanged"),
                        f"file was touched: {sync}")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"false flag (note: {(row.get('note') or '')[:300]})")

    def test_true_block_still_caught_full(self):
        """C_S03E11: a genuine block with a clean screen must still be caught in full."""
        row = _run_genuine("C_S03E11", "full")
        self.assertIn("anchor region(s)", row.get("sync_status") or "",
                      "overcorrection: the true block repair is gone")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")


if __name__ == "__main__":
    unittest.main()
