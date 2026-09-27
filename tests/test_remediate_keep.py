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


if __name__ == "__main__":
    unittest.main()
