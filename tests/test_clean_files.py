"""A verified-correct subtitle is never rewritten and never flagged, in either mode (the false-positive control).

The owner has confirmed that the ten Known Good subtitles are correct, so any change the pipeline makes to one is
a false positive. This is the case that once rewrote a correct file on 2 bad anchors out of 51. Runs the real
pipeline on the stored Whisper output and recorded alass answers; no media.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import e2e_matrix as M
from kg_env import MODEL, KgReplayCase, episodes
from verifyarr import db


def _run(slug, mode):
    fx = M.fixture(slug)
    video = M.media_dir(slug) / fx["video_name"]
    lang, segments = M.audio_evidence(MODEL, slug, fx)
    work = Path(tempfile.mkdtemp(prefix="clean_"))
    conn = db.connect(work / "t.db")
    try:
        cfg = M.cfg_for(conn, mode, "off", groq_model=MODEL)
        row, _after = M.run_one(work, video, M.subs_for(slug, fx), lang, segments, cfg, conn,
                                f"clean.{slug}.{mode}", mode, M.audio_cache_for(slug, video))
        return row
    finally:
        conn.close()


class CleanFileTests(KgReplayCase):
    def test_every_verified_episode_is_left_alone(self):
        slugs = episodes()
        self.assertEqual(len(slugs), 10)
        bad = []
        for slug in slugs:
            for mode in ("full", "sampled"):
                row = _run(slug, mode)
                sync = row.get("sync_status") or ""
                if row.get("correctness_flag") != "ok" or not sync.startswith(("already in sync", "left unchanged")):
                    bad.append(f"{slug} {mode}: {row.get('correctness_flag')} / {sync[:50]} / {(row.get('note') or '')[:120]}")
        self.assertEqual(bad, [], "a verified-correct subtitle was rewritten or flagged")


if __name__ == "__main__":
    unittest.main()
