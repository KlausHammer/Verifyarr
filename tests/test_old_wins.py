"""Ankre der modbeviser en kandidat: originalen beholdes, intet skrives.

_resolve_ambiguous_sync med mocket evidens (som RampDecisionUnitTests):
timing alene afgiver, intet Whisper. Single-blok tages som een blok
[0, inf); en vinder med >= 3 ankre > 2,5 s ude nedstemmes naar old er
klart bedre paa de faelles klip.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pysubs2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import pipeline as P


def _subs_span(seconds=3000.0, n=10):
    subs = pysubs2.SSAFile()
    for i in range(n):
        t = seconds * i / n
        subs.append(pysubs2.SSAEvent(
            start=int(t * 1000), end=int(t * 1000) + 1500, text=f"line {i}"))
    return subs


REGIONS = [223.8, 949.7, 1257.4, 1719.5, 2016.1, 2420.3, 2805.3, 3025.8]


def _resolve(new_res, old_res, blocks_res=None, single_block=False):
    """Kald _resolve_ambiguous_sync med givne residualer pr. klip."""
    new_subs, old_subs = _subs_span(), _subs_span()
    blocks_subs = _subs_span() if blocks_res is not None else None
    by_id = {id(new_subs): "new", id(old_subs): "old"}
    if blocks_subs is not None:
        by_id[id(blocks_subs)] = "blocks"
    resid = {"new": new_res, "old": old_res}
    if blocks_res is not None:
        resid["blocks"] = blocks_res

    def fake(conn, video, subs, lang, tl, cfg, score=False, **kw):
        key = by_id[id(subs)]
        samples = [{"start": t, "anchor": {"shift": s, "anchor_count": 3}}
                   for t, s in zip(REGIONS, resid[key])]
        if not score:
            return {"avg_score": None, "flag": None, "samples": samples}
        return {"avg_score": 0.80, "flag": "ok", "samples": samples}

    ambiguous = {"old_subs": old_subs, "orig_subs": old_subs, "new_subs": new_subs,
                 "max_shift_new": 8.4, "structural": False,
                 "single_block": single_block}
    if blocks_subs is not None:
        ambiguous.update(blocks_subs=blocks_subs, max_shift_blocks=12.0,
                         blocks_split_count=3, blocks_spread=20.0,
                         blocks_time_ranges=[(0, 800), (800, 1600), (1600, 3200)])
    else:
        ambiguous.update(blocks_time_ranges=[])
    result = {"avg_score": 0.80, "flag": "ok",
              "samples": [{"start": 223.8, "score": 0.80}], "audio_lang": "en",
              "swap_severity": None, "fps_points": [], "full_coverage": False}
    row = {"note": "", "sync_status": "fixed (Δ8.4s) [pending verification]",
           "sync_max_shift_s": 8.4, "sync_split_blocks": 1,
           "sync_block_spread_s": None}
    cfg = types.SimpleNamespace(whisper_mode="sampled", min_change_seconds=0.25,
                                backup_originals=False, fps_check_enabled=False)
    with tempfile.TemporaryDirectory() as td:
        sub_path = Path(td) / "t.srt"
        sub_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
        with mock.patch.object(P, "evaluate_against_cached_transcripts",
                               side_effect=fake):
            _subs, _res, _sev, winner = P._resolve_ambiguous_sync(
                None, Path("/tmp/vid.mkv"), sub_path, "en", cfg, Path(td),
                ambiguous, result, row)
        return winner, row, sub_path.read_text()


class OldWinsTests(unittest.TestCase):
    def test_single_block_old_wins_when_clearly_better(self):
        """Single-blok: new 8,5 s ude, old 0,7 s -- old beholdes."""
        new = [8.5, 8.2, 8.8, 8.1, 8.6, 8.4, 8.3, 8.7]
        old = [0.7, 0.6, 0.8, 0.5, 0.7, 0.6, 0.8, 0.7]
        winner, row, _disk = _resolve(new, old, single_block=True)
        self.assertEqual(winner, "old", row["note"][-300:])
        self.assertIn("already in sync", row["sync_status"])

    def test_single_block_good_fit_still_wins(self):
        """Single-blok: new 0,3 s, old 45 s -- new skrives som foer."""
        new = [0.3, 0.2, 0.4, 0.3, 0.2, 0.4, 0.3, 0.2]
        old = [45.0, 45.1, 44.9, 45.2, 45.0, 44.8, 45.1, 45.0]
        winner, row, _disk = _resolve(new, old, single_block=True)
        self.assertEqual(winner, "new", row["note"][-300:])
        self.assertTrue(row["sync_status"].startswith("fixed"))

    def test_bad_multiblock_winner_is_vetoed(self):
        """New/blocks >= 3 klip > 2,5 s, old klar bedre -- old beholdes.

        Old staar 5 s skidt i foerste blok (ikke 'fair' sejr), men new staar
        8,5 s skidt overalt: fittet er modbevist, veto redder originalen."""
        new = [8.5, 8.2, 8.8, 8.1, 8.6, 8.4, 8.3, 8.7]
        blocks = [12.3, 12.0, 12.5, 12.1, 12.4, 12.2, 12.6, 12.0]
        old = [5.0, 0.1, 0.3, 0.2, 0.1, 0.3, 0.2, 0.1]
        winner, row, _disk = _resolve(new, old, blocks_res=blocks)
        self.assertEqual(winner, "old", row["note"][-300:])
        self.assertTrue(row.pop("_vetoed_bad_fit", False),
                        "veto must mark the row so the caller flags SUSPECT")

    def test_good_winner_is_not_vetoed(self):
        """New ren (0,4 s), old daarlig (25 s) -- new skrives, ingen veto."""
        new = [0.4, 0.3, 0.5, 0.4, 0.3, 0.5, 0.4, 0.3]
        blocks = [0.5, 0.4, 0.6, 0.5, 0.4, 0.6, 0.5, 0.4]
        old = [25.0, 25.1, 24.9, 25.2, 25.0, 24.8, 25.1, 25.0]
        winner, row, _disk = _resolve(new, old, blocks_res=blocks)
        self.assertEqual(winner, "new", row["note"][-300:])
        self.assertNotIn("_vetoed_bad_fit", row)


if __name__ == "__main__":
    unittest.main()
