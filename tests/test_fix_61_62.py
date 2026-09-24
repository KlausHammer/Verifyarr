"""Fund 6.1 + 6.2: disk-sandhed ved presync-old-sejr og single-blok-verifikation.

Koerer den rigtige roerledning (M.run_one, frisk DB) som test_silent_rows,
saa en doed sti fejler hoejlydt. Begge tests fejler paa HEAD foer rettelsen
og bestaar efter.

6.1: uniform_neg full med presync + alass-multiblok hvor verifikationen
vaelger "old" (baseline). Foer: disken beholder originalen (-45s), mens
rapporten siger "already in sync", flag ok. Efter: baseline skrives og
rapporteres som fixed.

6.2: wrong_episode single-blok (alass finder eet blok-fit paa forkert
indhold). Foer: fittet skrives direkte, SUSPECT kommer for sent. Efter:
skrivningen holdes tilbage til indholdstjekket har talt, filen forbliver
uroert.

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
        """SH_S01E01 uniform_neg full: baseline-sejr skal paa disken som fixed,
        ikke "already in sync" paa en 45s-forkert fil."""
        row, rec = _run("SH_S01E01", "uniform_neg", mode="full")
        self.assertTrue((row.get("sync_status") or "").startswith("fixed"),
                         f"presync forkastet uden skrivning: {row.get('sync_status')} "
                         f"(note: {(row.get('note') or '')[:300]})")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"flag: {(row.get('note') or '')[:300]}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"disken stadig forkert: rec={rec.get('frac_le_1_0s')}")


@_needs_staging
class SingleBlockContentTests(unittest.TestCase):
    def test_single_block_wrong_content_stays_untouched(self):
        """C_S02E01 wrong_episode full: alass single-blok-fit paa forkert
        indhold skal holdes tilbage til indholdstjekket har talt. SUSPECT +
        uroert, ikke fixed."""
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
