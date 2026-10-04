"""Swap gate: many swapped lines are flagged, a few are only reported.

Timing-independent scan (text match within +-90 s + sequence dominance) on the
full transcript. At a rate >= 0.10 and >= 5 swapped out of >= 10 decided:
SUSPECT + untouched (fetch a new one), before any fix is written. A few swaps (n=6)
and clean files: as today (reported, not flagged).

Needs the staging data (VERIFYARR_TEST_DATA); skipped elsewhere.
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
from kg_env import MODEL, KgReplayCase
from verifyarr import db



def _corrupt_many_swaps(subs, frac=0.25):
    """25 % of the two-line cues swapped (cap signal first, the rest free)."""
    from verifyarr.line_order import (_split_two_lines, _cap_signal,
                                      heuristic_candidates, all_two_line_events)
    out = copy.deepcopy(subs)
    already = {c[0] for c in heuristic_candidates(out)}
    cands = [i for i, e in enumerate(out.events)
             if (p := _split_two_lines(e.text)) and _cap_signal(p[1], p[0])
             and i not in already]
    n = max(1, int(len(all_two_line_events(out)) * frac))
    if len(cands) < n:
        extra = [i for i, e in enumerate(out.events)
                 if _split_two_lines(e.text) and i not in already
                 and i not in cands]
        cands = cands + [i for i in extra if i not in cands]
    targets = cands[:n] if len(cands) <= n else [
        cands[int(k * len(cands) / n)] for k in range(n)]
    for i in targets:
        l1, l2 = _split_two_lines(out.events[i].text)
        out.events[i].text = f"{l2}\\N{l1}"
    return out, targets


def _run_row(slug, corrupted, mode="full", model=MODEL, segments=None, lang="en"):
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    if segments is None:
        lang, segments = M.audio_evidence(model, slug, fx)
        assert segments, f"no sweep segments for {slug}"
    work = Path(tempfile.mkdtemp(prefix="swapgate_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, "off", groq_model=model)
        cache = M.audio_cache_for(slug, video)
        row, _after = M.run_one(work, video, corrupted, lang, segments, cfg,
                                conn, "t", mode, cache)
        return row
    finally:
        conn.close()


class SwapGateTests(KgReplayCase):
    def test_many_swaps_full_flags_and_leaves_untouched(self):
        """25 % swapped lines: SUSPECT + untouched file, before any fix."""
        fx = M.fixture("SH_S01E01")
        subs = M.subs_for("SH_S01E01", fx)
        corrupted, _ = _corrupt_many_swaps(subs, 0.25)
        row = _run_row("SH_S01E01", corrupted, mode="full")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"no flag (note: {(row.get('note') or '')[:300]})")
        self.assertIn("Many swapped lines", row.get("note") or "")
        sync = row.get("sync_status") or ""
        self.assertTrue(sync.startswith(("already in sync", "left unchanged")),
                        f"rewrote a many-swap file: {sync}")

    def test_many_swaps_sampled_flags(self):
        """Sampled buys the full transcript and flags many swaps."""
        fx = M.fixture("SH_S01E01")
        subs = M.subs_for("SH_S01E01", fx)
        corrupted, _ = _corrupt_many_swaps(subs, 0.25)
        row = _run_row("SH_S01E01", corrupted, mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "SUSPECT",
                         f"no flag (note: {(row.get('note') or '')[:300]})")
        self.assertIn("Many swapped lines", row.get("note") or "")

    def test_few_swaps_full_not_flagged(self):
        """n=6 swaps: reported as today, not 'fetch new'."""
        fx = M.fixture("SH_S01E01")
        subs = M.subs_for("SH_S01E01", fx)
        corrupted, _, _ = M.corrupt_swap(
            copy.deepcopy(subs), random.Random("matrix-v1:SH_S01E01:swap"))
        row = _run_row("SH_S01E01", corrupted, mode="full")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"new flag on few swaps: {(row.get('note') or '')[-300:]}")
        self.assertNotIn("Many swapped lines", row.get("note") or "")

    def test_clean_full_not_flagged(self):
        """Clean file: no swap flag."""
        fx = M.fixture("SH_S01E01")
        subs = M.subs_for("SH_S01E01", fx)
        row = _run_row("SH_S01E01", copy.deepcopy(subs), mode="full")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"new flag on clean: {(row.get('note') or '')[-300:]}")
        self.assertNotIn("Many swapped lines", row.get("note") or "")

    def test_clean_sampled_not_flagged(self):
        """Clean file sampled: no swap flag."""
        fx = M.fixture("SH_S01E01")
        subs = M.subs_for("SH_S01E01", fx)
        row = _run_row("SH_S01E01", copy.deepcopy(subs), mode="sampled")
        self.assertEqual(row.get("correctness_flag"), "ok",
                         f"new flag on clean: {(row.get('note') or '')[-300:]}")
        self.assertNotIn("Many swapped lines", row.get("note") or "")


if __name__ == "__main__":
    unittest.main()
