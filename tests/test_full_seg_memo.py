"""The cached full transcript is filtered once per transcript, not once per caller."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, pipeline
from verifyarr.settings import Config


class FullSegMemoTests(unittest.TestCase):
    def test_filtered_once_and_refreshed_on_new_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "v.mkv"
            video.write_bytes(b"x")
            conn = db.connect(Path(td) / "t.db")
            cfg = Config.from_db(conn)
            provider, model = pipeline.full_transcript_cache_key(cfg)
            segs = [{"start": 1.0, "end": 2.0, "text": "hello there"}]
            db.save_full_transcript_cache(conn, video, "en", segs,
                                          stt_provider=provider, stt_model=model)
            pipeline._FULL_SEG_MEMO.clear()
            with mock.patch.object(pipeline, "_drop_nonspeech", side_effect=lambda s: s) as f:
                a = pipeline._cached_full_segments(conn, video, cfg)
                b = pipeline._cached_full_segments(conn, video, cfg)
                self.assertIs(a, b)
                self.assertEqual(f.call_count, 1)
                db.save_full_transcript_cache(conn, video, "en", segs + [
                    {"start": 3.0, "end": 4.0, "text": "again"}],
                    stt_provider=provider, stt_model=model)
                c = pipeline._cached_full_segments(conn, video, cfg)
                self.assertEqual(len(c), 2)
                self.assertEqual(f.call_count, 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
