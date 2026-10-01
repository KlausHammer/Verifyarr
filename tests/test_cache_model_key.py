"""Finding 1: the clip cache (video_transcript_cache) must carry the STT provider/model in its key.

A clip transcribed under model A must never be served as if it were model B's --
not even through the segments_json anchors in evaluate_against_cached_transcripts. At
the same time a model mismatch must never delete: a user who switches back and forth
between two models should not pay a re-transcription per switch (same rows, both
models side by side). Rows stored before the columns existed (NULL) are accepted as
they are until they age out -- the same NULL convention as video_mtime/video_size.

No staging data needed: pure DB tests, no alass/Whisper.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from verifyarr import db

# The DDL from before the fix (HEAD:verifyarr/db.py), frozen verbatim -- the migration
# test builds a "user with a year of cache" DB with it and lets connect() migrate it.
OLD_CLIP_CACHE_DDL = """
CREATE TABLE video_transcript_cache (
    video_path    TEXT NOT NULL,
    region_index  INTEGER NOT NULL,
    clip_start    REAL NOT NULL,
    audio_lang    TEXT,
    transcript    TEXT NOT NULL,
    segments_json TEXT,
    clip_seconds  REAL,
    video_mtime   REAL,
    video_size    INTEGER,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (video_path, region_index)
);
"""

A = ("groq", "whisper-large-v3")
B = ("groq", "whisper-large-v3-turbo")


def _mkdb():
    td = Path(tempfile.mkdtemp(prefix="cachekey_"))
    video = td / "ep.mkv"
    video.write_bytes(b"x" * 1024)
    return db.connect(td / "t.db"), video


class ModelKeyTests(unittest.TestCase):
    def test_clip_from_A_never_served_as_B(self):
        conn, video = _mkdb()
        try:
            db.save_transcript_cache(conn, video, 3, 120.0, "en", "dialogue A",
                                     segments=[{"start": 0.0, "end": 1.0, "text": "hi"}],
                                     stt_provider=A[0], stt_model=A[1])
            self.assertIsNone(
                db.get_cached_transcript(conn, video, 3, stt_provider=B[0], stt_model=B[1]),
                "model B was served model A's clip")
            self.assertEqual(
                db.get_cached_transcripts_for_video(conn, video, stt_provider=B[0],
                                                    stt_model=B[1]), [],
                "model B's evidence set contains model A's clip")
            self.assertIsNone(
                db.find_cached_transcript_between(conn, video, 100.0, 200.0,
                                                  stt_provider=B[0], stt_model=B[1]),
                "positional lookup under B found A's clip")
            hit = db.get_cached_transcript(conn, video, 3, stt_provider=A[0], stt_model=A[1])
            self.assertIsNotNone(hit, "model A's own clip is no longer hit")
            self.assertEqual(hit["transcript"], "dialogue A")
        finally:
            conn.close()

    def test_read_miss_deletes_nothing(self):
        conn, video = _mkdb()
        try:
            db.save_transcript_cache(conn, video, 0, 10.0, "en", "kept",
                                     stt_provider=A[0], stt_model=A[1])
            self.assertIsNone(db.get_cached_transcript(conn, video, 0, stt_provider=B[0],
                                                       stt_model=B[1]))
            n = conn.execute("SELECT COUNT(*) FROM video_transcript_cache").fetchone()[0]
            self.assertEqual(n, 1, "laesning under fremmed model slettede raekken")
            hit = db.get_cached_transcript(conn, video, 0, stt_provider=A[0], stt_model=A[1])
            self.assertIsNotNone(hit, "A's row gone after B's miss")
        finally:
            conn.close()

    def test_two_models_coexist_in_one_slot(self):
        conn, video = _mkdb()
        try:
            db.save_transcript_cache(conn, video, 1, 30.0, "en", "from A",
                                     stt_provider=A[0], stt_model=A[1])
            db.save_transcript_cache(conn, video, 1, 30.0, "en", "from B",
                                     stt_provider=B[0], stt_model=B[1])
            a = db.get_cached_transcript(conn, video, 1, stt_provider=A[0], stt_model=A[1])
            b = db.get_cached_transcript(conn, video, 1, stt_provider=B[0], stt_model=B[1])
            self.assertEqual(a["transcript"], "from A", "B's write ate A's row")
            self.assertEqual(b["transcript"], "from B")
        finally:
            conn.close()

    def test_legacy_null_rows_served_as_is(self):
        conn, video = _mkdb()
        try:
            conn.execute(
                "INSERT INTO video_transcript_cache (video_path, region_index, clip_start, "
                "audio_lang, transcript, created_at) VALUES (?, 2, 60.0, 'en', 'legacy', "
                "datetime('now'))", (str(video),))
            conn.commit()
            for prov, mod in (A, B):
                hit = db.get_cached_transcript(conn, video, 2, stt_provider=prov,
                                               stt_model=mod)
                self.assertIsNotNone(hit, f"NULL row not accepted under {mod}")
        finally:
            conn.close()

    def test_replaced_video_still_invalidates_every_model(self):
        conn, video = _mkdb()
        try:
            for prov, mod in (A, B):
                db.save_transcript_cache(conn, video, 0, 10.0, "en", f"from {mod}",
                                         stt_provider=prov, stt_model=mod)
            video.write_bytes(b"y" * 2048)  # another release, same path
            self.assertEqual(db.get_cached_transcripts_for_video(conn, video, stt_provider=A[0],
                                                                 stt_model=A[1]), [])
            n = conn.execute("SELECT COUNT(*) FROM video_transcript_cache").fetchone()[0]
            self.assertEqual(n, 0, "replaced video left clip rows behind")
        finally:
            conn.close()


class MigrationTests(unittest.TestCase):
    def test_old_db_migrates_without_losing_rows(self):
        td = Path(tempfile.mkdtemp(prefix="cachemig_"))
        video = td / "old.mkv"
        video.write_bytes(b"z" * 512)
        st = video.stat()
        db_path = td / "user.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(OLD_CLIP_CACHE_DDL)
        raw.execute(
            "INSERT INTO video_transcript_cache (video_path, region_index, clip_start, "
            "audio_lang, transcript, segments_json, clip_seconds, video_mtime, video_size, "
            "created_at) VALUES (?, 0, 5.0, 'en', 'year-old clip', "
            "'[{\"start\": 0.0, \"end\": 1.0, \"text\": \"hi\"}]', 30.0, ?, ?, "
            "datetime('now'))", (str(video), st.st_mtime, st.st_size))
        raw.execute(
            "INSERT INTO video_transcript_cache (video_path, region_index, clip_start, "
            "audio_lang, transcript, created_at) VALUES (?, 1, 40.0, 'en', 'older clip', "
            "datetime('now'))", (str(video),))
        raw.commit()
        raw.close()

        conn = db.connect(db_path)  # migrates on open
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(video_transcript_cache)")}
            self.assertIn("stt_provider", cols)
            self.assertIn("stt_model", cols)
            rows = conn.execute(
                "SELECT region_index, transcript FROM video_transcript_cache "
                "ORDER BY region_index").fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows],
                             [(0, "year-old clip"), (1, "older clip")],
                             "migreringen smed brugerdata vaek")
            hit = db.get_cached_transcript(conn, video, 0, stt_provider=A[0], stt_model=A[1])
            self.assertIsNotNone(hit, "migrated row not readable via the new API")
            self.assertEqual(hit["transcript"], "year-old clip")
            # New writes next to the migrated NULL rows.
            db.save_transcript_cache(conn, video, 0, 5.0, "en", "fresh A",
                                     stt_provider=A[0], stt_model=A[1])
            hit = db.get_cached_transcript(conn, video, 0, stt_provider=A[0], stt_model=A[1])
            self.assertEqual(hit["transcript"], "fresh A",
                             "NULL row shadowed the new keyed row")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class KeyIsRequiredTests(unittest.TestCase):
    """NULL != NULL in SQLite: a NULL key never matches ON CONFLICT, so every save would
    insert a duplicate instead of updating. The key must not be defaultable."""

    def test_save_without_key_is_a_call_site_error(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            with self.assertRaises(TypeError):
                db.save_transcript_cache(conn, Path("/v.mkv"), 0, 1.0, "en", "x")
            conn.close()

    def test_repeated_save_under_same_key_updates_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            for i in range(3):
                db.save_transcript_cache(conn, Path("/v.mkv"), 0, 1.0, "en", f"t{i}",
                                         stt_provider="local", stt_model="tiny.en")
            n = conn.execute("SELECT COUNT(*) FROM video_transcript_cache").fetchone()[0]
            self.assertEqual(n, 1)
            conn.close()
