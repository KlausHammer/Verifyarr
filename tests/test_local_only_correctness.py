"""Checks listen with local whisper.cpp only; cloud belongs to subtitle generation."""
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verifyarr import correctness, db, settings
from verifyarr.settings import Config


class LocalOnlyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")
        self.cfg = Config.from_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_no_cloud_speech_settings_remain_in_correctness(self):
        keys = {k for k, (g, _t, _d) in settings.SETTING_DEFS.items() if g == "correctness"}
        for gone in ("stt_provider", "use_local_whisper", "groq_api_key", "groq_model",
                     "openrouter_api_key", "openrouter_stt_model", "groq_llm_model",
                     "openrouter_llm_model"):
            self.assertNotIn(f"correctness.{gone}", keys)
        fields = {f.name for f in dataclasses.fields(Config)}
        self.assertFalse({"stt_provider", "use_local_whisper", "groq_api_key"} & fields)

    def test_transcription_is_always_local(self):
        with mock.patch.object(correctness, "_run_local_whisper", return_value={"segments": []}) as run:
            correctness.transcribe_verbose(self.cfg, Path("a.wav"), "en")
        run.assert_called_once()

    def test_cache_key_is_local(self):
        provider, model = db.full_transcript_cache_key(self.cfg)
        self.assertEqual(provider, "local")

    def test_translation_uses_the_generate_llm_settings(self):
        cfg = dataclasses.replace(self.cfg, generate_llm_provider="openrouter",
                                  generate_openrouter_api_key="k", generate_openrouter_llm_model="m1",
                                  generate_openrouter_llm_model_fallback=None)
        correctness._TRANSLATION_MEMO.clear()
        with mock.patch.object(correctness, "translate_text", return_value="hello") as tt:
            self.assertEqual(correctness.translate_to_english(cfg, "hej"), "hello")
        kw = tt.call_args.kwargs
        self.assertEqual((kw["provider"], kw["api_key"], kw["llm_model"]), ("openrouter", "k", "m1"))

    def test_missing_binary_means_cannot_check(self):
        cfg = dataclasses.replace(self.cfg, local_whisper_binary="/nonexistent/whisper-cli")
        self.assertFalse(cfg.has_stt_configured)


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    def test_two_songs_far_apart_are_two_windows(self):
        segs = [{"start": 30, "end": 40, "text": "real talk here now"},
                {"start": 500, "end": 510, "text": "more real talk here"}]
        music = [(10, 12), (900, 905)]
        secs, words = correctness.gap_speech(segs, 0, 1000, music)
        self.assertEqual(words, 8)

    def test_record_plays_is_not_a_music_mark(self):
        self.assertEqual(correctness.music_spans([{"start": 1, "end": 2, "text": "(record plays)"}]), [])
        self.assertEqual(len(correctness.music_spans([{"start": 1, "end": 2, "text": "(upbeat music)"}])), 1)

    def test_commentary_titled_english_is_not_the_english_track(self):
        from verifyarr import audiotrack
        self.assertFalse(audiotrack._is_english({"tags": {"title": "Director's Commentary (English)"}}))
        self.assertFalse(audiotrack._is_english({"tags": {"language": "jpn", "title": "English subs burned in"}}))
        self.assertTrue(audiotrack._is_english({"tags": {"language": "und", "title": "English (US)"}}))

    def test_unrelated_save_ignores_an_unchanged_bad_path(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            settings.set_settings_group(conn, "correctness", {"local_whisper_threads": 3})
            body = {"local_whisper_binary": settings.SETTING_DEFS["correctness.local_whisper_binary"][2],
                    "local_whisper_threads": 2}
            settings.set_settings_group(conn, "correctness", body)
            conn.close()
