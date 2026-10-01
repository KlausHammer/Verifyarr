"""_anchor_bad_samples: one definition of "the anchors condemn this file", shared by the veto path
and the anchor branch, so a vetoed file is never left neither flagged nor re-timed."""
import types
import unittest

from verifyarr import pipeline


def _result(n_bad):
    return {"samples": [{"start": 100 * i, "anchor": {"shift": 6.0}, "score": 0.9}
                        for i in range(n_bad)]}


def _cfg(**over):
    vals = dict(anchor_check_enabled=True, anchor_suspect_min_samples=2,
                block_spread_suspect_threshold_s=20.0, overlap_threshold=0.25)
    vals.update(over)
    return types.SimpleNamespace(**vals)


ROW = {"sync_block_spread_s": None}


class AnchorBadTests(unittest.TestCase):
    def test_two_bad_anchors_condemn_a_sampled_file(self):
        cfg = _cfg()
        ev = types.SimpleNamespace(whisper_mode="sampled")
        self.assertEqual(len(pipeline._anchor_bad_samples(cfg, ev, _result(2), ROW, None)), 2)

    def test_two_bad_anchors_are_noise_on_dense_full_evidence(self):
        # SH_S01E04: 2 of ~51. The veto path must agree, or the file is left unflagged.
        cfg = _cfg()
        ev = types.SimpleNamespace(whisper_mode="full")
        self.assertEqual(pipeline._anchor_bad_samples(cfg, ev, _result(2), ROW, None), [])
        self.assertEqual(len(pipeline._anchor_bad_samples(cfg, ev, _result(3), ROW, None)), 3)

    def test_a_resolved_winner_keeps_the_lower_bar(self):
        cfg = _cfg()
        ev = types.SimpleNamespace(whisper_mode="full")
        self.assertEqual(len(pipeline._anchor_bad_samples(cfg, ev, _result(2), ROW, "old")), 2)

    def test_disabled_check_finds_nothing(self):
        cfg = _cfg(anchor_check_enabled=False)
        ev = types.SimpleNamespace(whisper_mode="sampled")
        self.assertEqual(pipeline._anchor_bad_samples(cfg, ev, _result(5), ROW, None), [])


if __name__ == "__main__":
    unittest.main()
