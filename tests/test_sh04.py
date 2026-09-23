"""SH_S01E04 full maa ikke roeres: taet evidence kraever 3 vidner.

Brugeren har SET 22:47-23:47: underteksten er korrekt, men full-mode
omskrev filen paa praecis 2 daarlige ankre af ~51 (4 %). Sampled-barren
(2) er kalibreret paa ~5-16 klip; paa taet full-evidence gaelder 3.
C_S03E11 (aegte blok, 15+ vidner) skal stadig fanges.

Koerer den rigtige roerledning (M.run_one, frisk DB) paa de AEGTE
undertekster + transskripter som genuine.py. Needs the staging tree
(/mnt/c/...); skipped elsewhere.
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
    """Aegte undertekst + aegte transskript gennem roerledningen, frisk DB."""
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
        """Bekraeftet falsk positiv: korrekt fil omskrevet paa 2/51 ankre."""
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
        """C_S03E11: aegte blok med ren skaerm skal stadig fanges i full."""
        row = _run_genuine("C_S03E11", "full")
        self.assertIn("anchor region(s)", row.get("sync_status") or "",
                      "overcorrection: the true block repair is gone")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")


if __name__ == "__main__":
    unittest.main()
