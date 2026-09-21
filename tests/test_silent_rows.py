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


def _run(slug, scenario, mode="sampled", audio="on"):
    """Matrix-faithful single row: seeded corruption through M.run_one on a fresh DB."""
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    if not video.exists():
        raise unittest.SkipTest(f"no video for {slug}")
    lang, segments = M.audio_evidence(MODEL, slug, fx)
    assert segments, f"no sweep segments for {slug}"
    orig = M.subs_for(slug, fx)
    corrupted, _, _ = M.SCENARIOS[scenario](
        copy.deepcopy(orig), random.Random(f"matrix-v1:{slug}:{scenario}"))
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
        """C_S03E03 full: resync fikser naesten (0.882) men fejlplacerer snit --
        resten skal advare, ikke glide stille igennem. Forbedringen beholdes."""
        row, rec = _run("C_S03E03", "piecewise", mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        self.assertIn("anchor region(s)", row.get("sync_status") or "",
                      "resync fixup must stay on disk; only the verdict changes")
        self.assertIn("REMAINS", row.get("note") or "")
        self.assertGreater(rec.get("frac_le_1_0s"), 0.85)

    def test_good_resync_stays_ok(self):
        """SH_S01E01 full: resync naar 0.964 -- B1b's re-verdict maa ikke
        konvertere velfungerende rettelser til advarsler."""
        row, rec = _run("SH_S01E01", "piecewise", mode="full")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"regression: rettet -> advaret ({(row.get('note') or '')[:300]})")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90)

    def test_lone_huge_anchor_warns(self):
        """SH_S01E01 sampled: alass' 4-blok-fit er 25s galt i een blok, men
        sparsom sampling giver kun EET vidne -- min_samples undertrykker det,
        saa filen gik stille igennem (0.855). Et 10s+ vidne paa multiblok-form
        skal route til anker-grenen (fix-verificeret eller advarsel)."""
        row, rec = _run("SH_S01E01", "piecewise", mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"silent again (note: {(row.get('note') or '')[:300]})")
        self.assertLess(rec.get("frac_le_1_0s"), 0.90)


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
