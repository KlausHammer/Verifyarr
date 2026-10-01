"""Findings 6.1 + 6.2: what ends up on disk when the presync baseline wins, and single-block verification.

Runs the real pipeline (M.run_one, fresh DB) like test_silent_rows, so a dead
path fails loudly. Both tests failed on HEAD before the fix and pass after it.

6.1: uniform_neg full with presync + alass multi-block where verification
picks "old" (the baseline). Before: the disk keeps the original (-45 s) while
the report says "already in sync", flag ok. After: the baseline is written and
reported as fixed.

6.2: wrong_episode single block (alass finds one block fit on wrong
content). Before: the fit is written directly, SUSPECT comes too late. After:
the write is held back until the content check has spoken, the file stays
untouched.

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
from verifyarr import db

MODEL = "tiny.en-greedy-cpu"
STAGING_OK = (
    M.SWEEP.exists()
    and (M.SWEEP / MODEL / "SH_S01E01.json").exists()
    and (M.SWEEP / MODEL / "C_S02E01.json").exists()
)
_needs_staging = unittest.skipUnless(STAGING_OK, "needs whisper_gpu_staging sweep data")


def _run(slug, scenario, mode="full", audio="on"):
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    if not video.exists():
        raise unittest.SkipTest(f"no video for {slug}")
    lang, segments = M.audio_evidence(MODEL, slug, fx)
    assert segments, f"no sweep segments for {slug}"
    orig = M.subs_for(slug, fx)
    rng = random.Random(f"matrix-v1:{slug}:{scenario}")
    if scenario == "wrong_episode":
        corrupted, kept, _ = M.corrupt_wrong_episode(copy.deepcopy(orig), rng, slug)
    else:
        corrupted, kept, _ = M.SCENARIOS[scenario](copy.deepcopy(orig), rng)
    work = Path(tempfile.mkdtemp(prefix="fix6162_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, audio, groq_model=MODEL)
        cache = M.audio_cache_for(slug, video)
        row, after = M.run_one(work, video, corrupted, lang, segments, cfg,
                               conn, "t", mode, cache)
        rec = M.summarize(M.timing_errors(list(orig.events), list(after.events), kept))
        return row, rec
    finally:
        conn.close()


@_needs_staging
class PresyncBaselineTests(unittest.TestCase):
    def test_presync_winner_is_written_not_reported_in_sync(self):
        """SH_S01E01 uniform_neg full: a baseline win must land on disk as fixed,
        not "already in sync" on a file that is 45 s off."""
        row, rec = _run("SH_S01E01", "uniform_neg", mode="full")
        self.assertTrue((row.get("sync_status") or "").startswith("fixed"),
                         f"presync discarded without writing: {row.get('sync_status')} "
                         f"(note: {(row.get('note') or '')[:300]})")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"flag: {(row.get('note') or '')[:300]}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"disk still wrong: rec={rec.get('frac_le_1_0s')}")


@_needs_staging
class SingleBlockContentTests(unittest.TestCase):
    def test_single_block_wrong_content_stays_untouched(self):
        """C_S02E01 wrong_episode full: an alass single-block fit on wrong
        content must be held back until the content check has spoken. SUSPECT +
        untouched, not fixed."""
        row, _rec = _run("C_S02E01", "wrong_episode", mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"flag: {(row.get('note') or '')[:300]}")
        s = row.get("sync_status") or ""
        untouched = (s.startswith("already in sync") or s.startswith("left unchanged")) \
            and row.get("line_order_fixed") in (None, 0)
        self.assertTrue(untouched,
                        f"forkert indhold omskrevet single-blok: {s} "
                        f"lo_fixed={row.get('line_order_fixed')}")


if __name__ == "__main__":
    unittest.main()
