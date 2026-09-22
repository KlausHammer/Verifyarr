"""Fund 1: klip-cachen (video_transcript_cache) skal baere STT provider/model i sin noegle.

Et klip transskriberet under model A maa aldrig serveres som om det var model B's --
heller ikke via segments_json-ankrene i evaluate_against_cached_transcripts. Samtidig
maa et model-mismatch aldrig slette: en bruger der skifter frem og tilbage mellem to
modeller skal ikke betale en re-transskribering pr. skift (samme raekker, begge
modeller side om side). Raekker gemt foer kolonnerne fandtes (NULL) accepteres som de
er, indtil de aelder ud -- samme NULL-konvention som video_mtime/video_size.

Ingen staging-noedvendighed: rene DB-tests, ingen alass/Whisper.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from verifyarr import db

# DDL'en fra foer rettelsen (HEAD:verifyarr/db.py), frosset ordret -- migrations-
# testen bygger en "bruger med et aar i cachen"-DB med den og lader connect()
# migrere den.
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
                "model B fik serveret model A's klip")
            self.assertEqual(
                db.get_cached_transcripts_for_video(conn, video, stt_provider=B[0],
                                                    stt_model=B[1]), [],
                "model B's evidence-set indeholder model A's klip")
            self.assertIsNone(
                db.find_cached_transcript_between(conn, video, 100.0, 200.0,
                                                  stt_provider=B[0], stt_model=B[1]),
                "positionelt opslag under B fandt A's klip")
            hit = db.get_cached_transcript(conn, video, 3, stt_provider=A[0], stt_model=A[1])
            self.assertIsNotNone(hit, "model A's eget klip ramt ikke laengere")
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
            self.assertIsNotNone(hit, "A's raekke vaek efter B's miss")
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
            self.assertEqual(a["transcript"], "from A", "B's skrivning aad A's raekke")
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
                self.assertIsNotNone(hit, f"NULL-raekke ikke accepteret under {mod}")
        finally:
            conn.close()

    def test_replaced_video_still_invalidates_every_model(self):
        conn, video = _mkdb()
        try:
            for prov, mod in (A, B):
                db.save_transcript_cache(conn, video, 0, 10.0, "en", f"from {mod}",
                                         stt_provider=prov, stt_model=mod)
            video.write_bytes(b"y" * 2048)  # andet release, samme sti
            self.assertEqual(db.get_cached_transcripts_for_video(conn, video, stt_provider=A[0],
                                                                 stt_model=A[1]), [])
            n = conn.execute("SELECT COUNT(*) FROM video_transcript_cache").fetchone()[0]
            self.assertEqual(n, 0, "udskiftet video efterlod klip-raekker")
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

        conn = db.connect(db_path)  # migrerer ved aabning
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
            self.assertIsNotNone(hit, "migreret raekke ikke laesbar via ny API")
            self.assertEqual(hit["transcript"], "year-old clip")
            # Nye skrivninger ved siden af de migrerede NULL-raekker.
            db.save_transcript_cache(conn, video, 0, 5.0, "en", "fresh A",
                                     stt_provider=A[0], stt_model=A[1])
            hit = db.get_cached_transcript(conn, video, 0, stt_provider=A[0], stt_model=A[1])
            self.assertEqual(hit["transcript"], "fresh A",
                             "NULL-raekke skyggede for den nye noeglede raekke")
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
