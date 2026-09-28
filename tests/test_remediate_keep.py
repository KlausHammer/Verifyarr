"""Failed remediation keeps the original, still flagged (user's rule)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import pipeline


def _cfg():
    return SimpleNamespace(dry_run=False, backup_originals=False, bazarr_path_map="")


class KeepOriginalTests(unittest.TestCase):
    def _run(self, outcome):
        with tempfile.TemporaryDirectory() as td:
            sub = Path(td) / "ep.en.srt"
            sub.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            meta = {"kind": "episode", "series_id": 1, "episode_id": 2, "subs_id": "x"}

            def blacklist(cfg, m):
                sub.unlink()  # Bazarr deletes the file it blacklists
                return True

            with mock.patch.object(pipeline, "bazarr_blacklist", blacklist), \
                 mock.patch.object(pipeline, "bazarr_map_path", lambda cfg, p: str(p)), \
                 mock.patch.object(pipeline, "remediate_suspect", lambda *a, **k: outcome):
                msg = pipeline.handle_suspect(sub, Path(td) / "ep.mkv", _cfg(), Path(td), "en",
                                              meta, None, "remediate")
            return msg, sub.exists() and sub.read_bytes()

    def test_no_replacement_puts_the_original_back(self):
        msg, content = self._run("no usable subtitle found after 4 attempt(s) (1 auto + 3 manual). ")
        self.assertIn("original kept, marked wrong", msg)
        self.assertEqual(content, b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")

    def test_verified_replacement_is_left_alone(self):
        msg, content = self._run(pipeline.REMEDIATED_PREFIX + "manual attempt 1/3: passed")
        self.assertNotIn("original kept", msg)
        self.assertFalse(content)


class ReplacementVerdictTests(unittest.TestCase):
    def test_a_passed_replacement_keeps_its_own_verdict(self):
        """The old SUSPECT verdict must not overwrite the row the replacement saved."""
        import copy
        import dataclasses
        import random
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import e2e_matrix as M
        from verifyarr import db

        slug, model = "SH_S01E01", "tiny.en-greedy-cpu"
        if not (M.SWEEP / model / f"{slug}.json").exists():
            self.skipTest("needs whisper_gpu_staging sweep data")
        fx = M.fixture(slug)
        video = M.media_dir(slug) / fx["video_name"]
        if not video.exists():
            self.skipTest(f"no video for {slug}")
        lang, segments = M.audio_evidence(model, slug, fx)
        corrupted, _, _ = M.corrupt_wrong_episode(
            copy.deepcopy(M.subs_for(slug, fx)), random.Random("remediate"), slug)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            conn = db.connect(work / "t.db")
            cfg = dataclasses.replace(M.cfg_for(conn, "sampled", "on", groq_model=model),
                                      correctness_auto_action="remediate")

            def replaced(subtitle_path, video_path, *a, conn=None, run_id=None, **k):
                db.update_state(conn, video_path, subtitle_path,
                                {"lang": "en", "sync_status": "already in sync",
                                 "correctness_flag": "ok", "note": "replacement"}, run_id=run_id)
                return "blacklisted in Bazarr; " + pipeline.REMEDIATED_PREFIX + "attempt 1: passed"

            with mock.patch.object(pipeline, "handle_suspect", replaced):
                M.run_one(work, video, corrupted, lang, segments, cfg, conn, "t", "sampled",
                          M.audio_cache_for(slug, video))
            row = conn.execute("SELECT correctness_flag, reason, note, auto_action FROM files "
                               "WHERE subtitle_path IS NOT NULL").fetchone()
            conn.close()
        self.assertEqual(row["correctness_flag"], "ok", row["note"])
        self.assertIsNone(row["reason"])
        self.assertEqual(row["note"], "replacement")
        self.assertIn(pipeline.REMEDIATED_PREFIX, row["auto_action"])


if __name__ == "__main__":
    unittest.main()
