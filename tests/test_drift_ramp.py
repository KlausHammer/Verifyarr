"""Rampe-redning: efter eskalering skelnes RAMPE (drift) fra TRIN (blokke).

Naar alass møder en driftende fil uden presync, laver den en blok-trappe
(4-8 blokke), og resolutionen beholder trappen: den passer lokalt bedre end
et enkelt offset baade paa ankre og indhold. Men trappen er forkert form --
paa kandidat 'new's fulde pulje ligger rampen stadig som en ren linje
(tilt -65s, rho -1.00, keep 0.97 paa SH_S01E01 drift). Saa naar den fulde
pulje viser en ren linje, beholdes 'new', og den eksisterende stretch-fix
retter raten. TRIN-puljer (blokfejl) gaar den gamle vej uændret.

Rørledningstestene kræver staging-træet (/mnt/c/...) som matricen;
enhedstestene mocker kun evidenslaget, ikke beslutningen.
"""
from __future__ import annotations

import copy
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pysubs2

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
from verifyarr import db
from verifyarr import pipeline as P

MODEL = "tiny.en-greedy-cpu"
STAGING_OK = (
    M.SWEEP.exists()
    and (M.SWEEP / MODEL / "SH_S01E01.json").exists()
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
    work = Path(tempfile.mkdtemp(prefix="driftramp_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, audio, groq_model=MODEL)
        cache = M.audio_cache_for(slug, video)
        row, after = M.run_one(work, video, corrupted, lang, segments, cfg,
                               conn, "t", mode, cache)
        rec = M.summarize(M.timing_errors(list(orig.events), list(after.events),
                                           kept if kept else None))
        return row, rec
    finally:
        conn.close()


@_needs_staging
class DriftRampPipelineTests(unittest.TestCase):
    def test_long_episode_drift_sampled(self):
        """SH_S01E01 drift sampled: presync ser kun 22/28 punkter (keep 0.86)."""
        row, rec = _run("SH_S01E01", "drift")
        self.assertIn("stretch", row.get("sync_status") or "",
                      f"no rate fix applied: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"still broken: rec={rec.get('frac_le_1_0s')}")

    def test_short_episode_drift_offset_sampled(self):
        """SH_S01E01 drift_offset sampled: rate + forsinkelse rettes."""
        row, rec = _run("SH_S01E01", "drift_offset")
        self.assertIn("stretch", row.get("sync_status") or "",
                      f"no rate fix applied: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"still broken: rec={rec.get('frac_le_1_0s')}")

    def test_drift_swap_sampled(self):
        """SH_S01E03 drift_swap sampled: swap + drift, rampen skal stadig ses."""
        row, rec = _run("SH_S01E03", "drift_swap")
        self.assertIn("stretch", row.get("sync_status") or "",
                      f"no rate fix applied: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"still broken: rec={rec.get('frac_le_1_0s')}")

    def test_drift_offset_full(self):
        """SH_S01E01 drift_offset full: eneste advarede full-celle (rec 0,46)."""
        row, rec = _run("SH_S01E01", "drift_offset", mode="full")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"still broken: rec={rec.get('frac_le_1_0s')}")

    def test_long_episode_drift_suspect_content(self):
        """SH_S01E06 drift sampled: rampen er ren, men new's indhold dumper."""
        row, rec = _run("SH_S01E06", "drift")
        self.assertIn("stretch", row.get("sync_status") or "",
                      f"no rate fix applied: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"still broken: rec={rec.get('frac_le_1_0s')}")

    def test_step_file_still_keeps_blocks(self):
        """SH_S01E02 piecewise sampled: TRIN maa ikke ligne en rampe."""
        row, rec = _run("SH_S01E02", "piecewise")
        self.assertIn("sync block(s)", row.get("sync_status") or "",
                      f"block fit lost: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.90,
                                f"block repair broken: rec={rec.get('frac_le_1_0s')}")

    def test_cut_file_still_keeps_blocks(self):
        """SH_S01E04 cut_version sampled: 2-blok-formen er den farlige nabo."""
        row, rec = _run("SH_S01E04", "cut_version")
        self.assertIn("sync block(s)", row.get("sync_status") or "",
                      f"block fit lost: {row.get('sync_status')}")
        self.assertTrue(rec.get("frac_le_1_0s") >= 0.90
                        or row.get("correctness_flag") == "SUSPECT",
                        f"went silent: rec={rec.get('frac_le_1_0s')}")

    def test_presync_fixed_file_not_restretched(self):
        """SH_S01E01 uniform_neg full: rent skift rettes uden rescue."""
        row, rec = _run("SH_S01E01", "uniform_neg", mode="full")
        self.assertNotIn("Ramp rescue", row.get("note") or "")
        self.assertNotIn("stretch", row.get("sync_status") or "",
                         f"restretched a fixed file: {row.get('sync_status')}")
        self.assertGreaterEqual(rec.get("frac_le_1_0s"), 0.99,
                                f"perfect fix smeared: rec={rec.get('frac_le_1_0s')}")

    def test_lucky_baseline_cut_not_rescued(self):
        """SH_S01E02 cut_version sampled: falsk rampe maa ikke udløse rescue."""
        row, rec = _run("SH_S01E02", "cut_version")
        self.assertNotIn("Ramp rescue", row.get("note") or "")
        self.assertNotIn("stretch", row.get("sync_status") or "",
                         f"rate fix on a cut file: {row.get('sync_status')}")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT")


def _subs_span(seconds=3000.0, n=10):
    subs = pysubs2.SSAFile()
    for i in range(n):
        t = seconds * i / n
        subs.append(pysubs2.SSAEvent(
            start=int(t * 1000), end=int(t * 1000) + 1500, text=f"line {i}"))
    return subs


def _ramp_pool(rate=0.02, span=3000.0, n=200, noise=0.2, seed=7):
    """(audio, subtitle)-punkter for en fil der er rate-forskudt: ren linje."""
    rng = random.Random(seed)
    return [(a, a * (1 + rate) + rng.gauss(0, noise))
            for i in range(n) for a in [span * i / n]]


def _step_pool(seed=7):
    """(audio, subtitle)-punkter for 6 blokke: trin, ingen linje."""
    rng = random.Random(seed)
    offs = [5.0, -8.0, 12.0, -6.0, 9.0, -11.0]
    pts = []
    for b, off in enumerate(offs):
        for i in range(33):
            a = b * 500.0 + i * 15.0
            pts.append((a, a + off + rng.gauss(0, 0.3)))
    return pts


class RampDecisionUnitTests(unittest.TestCase):
    """_resolve_ambiguous_sync med mocket evidens: beslutningen øves, ikke data."""

    REGIONS = [223.8, 949.7, 1257.4, 1719.5, 2016.1, 2420.3, 2805.3, 3025.8]
    NEW_RES = [14.9, -0.4, -7.5, -3.7, 8.0, 6.1, -5.9, 12.0]
    BLOCKS_RES = [0.2, -0.3, 0.1, 0.4, -0.2, 0.3, -0.1, 0.2]
    OLD_RES = [25.0, 26.1, 24.3, 27.2, 25.8, 26.5, 24.9, 25.4]

    def _resolve(self, pool, full_coverage=True, flag="ok", scores=None):
        new_subs, blocks_subs, old_subs = _subs_span(), _subs_span(), _subs_span()
        by_id = {id(new_subs): "new", id(blocks_subs): "blocks", id(old_subs): "old"}
        resid = {"new": self.NEW_RES, "blocks": self.BLOCKS_RES, "old": self.OLD_RES}
        scores = scores or {"new": (0.59, "ok"), "blocks": (0.69, "ok"), "old": (0.54, "ok")}
        calls = []

        def fake(conn, video, subs, lang, tl, cfg, score=False, **kw):
            key = by_id[id(subs)]
            calls.append((key, score))
            samples = [{"start": t, "anchor": {"shift": s, "anchor_count": 3}}
                       for t, s in zip(self.REGIONS, resid[key])]
            if not score:
                return {"avg_score": None, "flag": None, "samples": samples}
            avg, fl = scores[key]
            return {"avg_score": avg, "flag": fl, "samples": samples}

        ambiguous = {"old_subs": old_subs, "new_subs": new_subs, "blocks_subs": blocks_subs,
                     "max_shift_new": 55.5, "max_shift_blocks": 60.0,
                     "blocks_split_count": 4, "blocks_spread": 50.0,
                     "blocks_time_ranges": [(0, 800), (800, 1600), (1600, 2400), (2400, 3200)],
                     "structural": False}
        result = {"avg_score": 0.59, "flag": flag,
                  "samples": [{"start": 223.8, "score": 0.59}], "audio_lang": "en",
                  "swap_severity": None, "fps_points": pool, "full_coverage": full_coverage}
        row = {"note": "", "sync_status": "fixed (Δ55.5s) [pending verification]",
               "sync_max_shift_s": 55.5, "sync_split_blocks": 1, "sync_block_spread_s": None}
        cfg = types.SimpleNamespace(whisper_mode="sampled", min_change_seconds=0.25,
                                    backup_originals=False, fps_check_enabled=True)
        with tempfile.TemporaryDirectory() as td:
            sub_path = Path(td) / "t.srt"
            sub_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
            with mock.patch.object(P, "evaluate_against_cached_transcripts",
                                   side_effect=fake):
                _subs, _res, _sev, winner = P._resolve_ambiguous_sync(
                    None, Path("/tmp/vid.mkv"), sub_path, "en", cfg, Path(td),
                    ambiguous, result, row)
            return winner, row, calls

    def test_ramp_prefers_new(self):
        winner, row, calls = self._resolve(_ramp_pool())
        self.assertEqual(winner, "new")
        self.assertIn("Ramp rescue", row["note"])
        # Indholds-scoring af rivalerne springes over: beslutningen er formen.
        self.assertFalse([c for c in calls if c[1] is True],
                         f"paid content scoring despite ramp: {calls}")

    def test_steps_keep_blocks(self):
        winner, row, _ = self._resolve(_step_pool())
        self.assertEqual(winner, "blocks")
        self.assertNotIn("Ramp rescue", row["note"])

    def test_sampled_pool_cannot_rescue(self):
        winner, row, _ = self._resolve(_ramp_pool(), full_coverage=False)
        self.assertEqual(winner, "blocks")
        self.assertNotIn("Ramp rescue", row["note"])

    def test_overwhelming_linearity_rescues(self):
        """Keep 0.87 under baren, men rho ~1 og gain stor: second path."""
        from verifyarr.subtitles import stretch_probe
        pool = _ramp_pool_with_mismatches()
        probe = stretch_probe([(a, a - s) for a, s in pool])
        self.assertLess(probe["keep_frac"], 0.90, "pool must miss the strict bar")
        winner, row, _ = self._resolve(pool)
        self.assertEqual(winner, "new")
        self.assertIn("Ramp rescue", row["note"])

    def test_verified_baseline_suppresses_rescue(self):
        """Ren rampe, men old verificerer 0,2s: sammenligningen finder old."""
        scores = {"new": (0.44, "ok"), "blocks": (0.44, "ok"), "old": (0.93, "ok")}
        with mock.patch.object(RampDecisionUnitTests, "OLD_RES",
                               [0.2, -0.3, 0.1, 0.4, -0.2, 0.3, -0.1, 0.2]):
            winner, row, _ = self._resolve(_ramp_pool(), scores=scores)
        self.assertEqual(winner, "old")
        self.assertNotIn("Ramp rescue", row["note"])

    def test_ramp_via_old_content(self):
        """New SUSPECT + old ok: rampen sprænger new's vinduer; teksten er god."""
        mixed = {"new": (0.24, "SUSPECT"), "blocks": (0.72, "ok"), "old": (0.59, "ok")}
        winner, row, calls = self._resolve(_ramp_pool(), flag="SUSPECT", scores=mixed)
        self.assertEqual(winner, "new")
        self.assertIn("Ramp rescue", row["note"])
        self.assertEqual([c for c in calls if c == ("old", True)], [("old", True)])
        self.assertFalse([c for c in calls if c[0] == "blocks" and c[1] is True],
                         f"blocks scored despite rescue: {calls}")

    def test_unmatched_content_never_rescued(self):
        bad = {"new": (0.07, "SUSPECT"), "blocks": (0.06, "SUSPECT"), "old": (0.07, "SUSPECT")}
        winner, row, calls = self._resolve(_ramp_pool(), flag="SUSPECT", scores=bad)
        self.assertEqual(winner, "old")
        self.assertIn("no candidate matched", row["sync_status"])
        self.assertNotIn("Ramp rescue", row["note"])
        # Old blev scoret én gang (redningsforsøget) og genbrugt, ikke scoret om.
        self.assertEqual(len([c for c in calls if c == ("old", True)]), 1)


def _ramp_pool_with_mismatches(rate=0.0636, span=1300.0, n=200, n_wild=30, seed=9):
    """Rampe + strø-mismatches: keep ~0.87, men rho ~1 og gain stor."""
    rng = random.Random(seed)
    pts = [(a, a * (1 + rate) + rng.gauss(0, 0.3))
           for i in range(n) for a in [span * i / n]]
    for i in range(n_wild):
        a = rng.uniform(0, span)
        pts.append((a, a * (1 + rate) + rng.choice((-1, 1)) * rng.uniform(15, 30)))
    return pts


class QuartileAbstentionUnitTests(unittest.TestCase):
    """Tomme kvartiler stemmer ikke: fravær af data er ikke modbevis."""

    def test_lone_mismatch_in_empty_quarter_abstains(self):
        from verifyarr.subtitles import max_quartile_residual_after
        rng = random.Random(7)
        # Delvis pulje som SH_S01E06 drift: sene punkter paa linjen, ét vildt
        # mismatch tidligt hvor den sande linje laa uden for ankervinduet.
        pool = [(a, a * 1.0636 - 181.0 + rng.gauss(0, 0.3))
                for i in range(58) for a in [1900.0 + i * 22.0]]
        pool.append((20.0, 0.3))  # mismatch, residual -160s
        ratio, offset = 1.0 / 1.0636, 181.0
        self.assertLess(max_quartile_residual_after(pool, ratio, offset), 1.5)


if __name__ == "__main__":
    unittest.main()
