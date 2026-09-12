"""What survives a generation run: the row that gets persisted and reported, the transcript
cache's identity rules, the retry/quota bookkeeping, and writing the file itself.

These are the integration-shaped seams of the feature -- each one sits between two modules, which
is exactly where a unit test of either side alone proves nothing. The first test here is a
regression test for a bug that shipped: finish_generated left pysubs2 objects in the row, and
reports.write_report then failed to JSON-encode it -- AFTER the subtitle had been written and the
database updated, so a fully successful generation was recorded as a failed run.

Run: python3 -m unittest discover -s tests
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pysubs2

from verifyarr import db, fileops, pipeline
from verifyarr.settings import Config


def _subs(*offsets_ms) -> pysubs2.SSAFile:
    subs = pysubs2.SSAFile()
    for i, start in enumerate(offsets_ms):
        subs.append(pysubs2.SSAEvent(start=start, end=start + 1000, text=f"line {i}"))
    return subs


class TempAppCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = db.connect(self.root / "verifyarr.db")
        self.cfg = Config.from_db(self.conn)
        self.video = self.root / "Ocean's Eleven (2001).mkv"
        self.video.write_bytes(b"x" * 5000)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)


class FinishGenerated(TempAppCase):
    def _row(self, pending=True, max_shift=0.4):
        subtitle = self.video.with_name(f"{self.video.stem}.da.srt")
        _subs(1000, 5000).save(str(subtitle), format_="srt")
        row = {"video": str(self.video), "subtitle": str(subtitle), "lang": "da",
               "sync_status": "fixed (Δ0.4s) [pending verification]", "sync_max_shift_s": None,
               "structural_change": False, "sync_split_blocks": None, "sync_block_spread_s": None,
               "correctness_flag": "-", "correctness_avg_score": None, "note": "", "auto_action": "-"}
        if pending:
            row["_ambiguous_sync"] = {
                "old_subs": _subs(1000, 5000), "new_subs": _subs(1400, 5400),
                "max_shift_new": max_shift, "blocks_subs": _subs(1400, 5600),
                "max_shift_blocks": max_shift, "blocks_split_count": 2, "blocks_spread": 0.2,
                "blocks_time_ranges": [], "structural": False,
            }
        return subtitle, row

    def test_row_is_json_serializable_afterwards(self):
        # REGRESSION: the deferred-sync payload holds live pysubs2 objects. Leaving it in the row
        # made reports.write_report raise, which marked an otherwise successful run "failed".
        subtitle, row = self._row()
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertNotIn("_ambiguous_sync", out)
        json.dumps(out)  # must not raise

    def test_the_deferred_fix_actually_reaches_disk(self):
        # The whole justification for skipping the correctness check is that alass still runs as
        # a sanity check of our own output. That is only true if its result is applied.
        subtitle, row = self._row()
        pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertEqual([e.start for e in pysubs2.load(str(subtitle))], [1400, 5400])

    def test_status_no_longer_claims_to_be_pending(self):
        subtitle, row = self._row()
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertNotIn("pending", out["sync_status"])

    def test_flag_is_generated_not_a_passed_check(self):
        subtitle, row = self._row()
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertEqual(out["correctness_flag"], "generated")
        # ...and it must not be written to the match-rate history, which only tracks real checks.
        history = self.conn.execute("SELECT COUNT(*) FROM correctness_history").fetchone()[0]
        self.assertEqual(history, 0)

    def test_a_large_alass_shift_is_surfaced(self):
        # Cues timed from this video's own audio should already line up with it. A big shift
        # means the transcription's timeline was wrong, which is worth saying out loud since
        # nothing else checks a generated file.
        subtitle, row = self._row(max_shift=42.0)
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertIn("42.0s", out["note"])

    def test_a_small_alass_shift_is_not_flagged(self):
        subtitle, row = self._row(max_shift=0.4)
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertNotIn("NOTE:", out["note"])

    def test_the_missing_placeholder_row_is_cleared(self):
        # Otherwise it coexists with the real row (different unique indexes), leaving a
        # "Generate" button on a video that just got exactly what it asked for.
        db.mark_missing(self.conn, self.video, "da", self.root)
        subtitle, row = self._row()
        pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE sync_status = 'missing'").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_works_when_nothing_was_deferred(self):
        subtitle, row = self._row(pending=False)
        row["sync_status"] = "already in sync"
        out = pipeline.finish_generated(self.video, subtitle, self.cfg, self.conn, row)
        self.assertEqual(out["sync_status"], "already in sync")
        json.dumps(out)


class FullTranscriptCache(TempAppCase):
    SEGMENTS = [{"start": 1.0, "end": 2.0, "text": "hello"}]

    def setUp(self):
        super().setUp()
        db.save_full_transcript_cache(self.conn, self.video, "en", self.SEGMENTS,
                                      stt_provider="groq", stt_model="whisper-large-v3")

    def test_hit_when_everything_matches(self):
        hit = db.get_full_transcript_cache(self.conn, self.video, "groq", "whisper-large-v3")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["segments"], self.SEGMENTS)

    def test_miss_after_switching_provider_or_model(self):
        # A user who switched provider did so BECAUSE the transcript was poor. Serving them the
        # cached one for another 90 days makes the setting look broken.
        self.assertIsNone(db.get_full_transcript_cache(self.conn, self.video, "cloudflare", "whisper-large-v3"))
        self.assertIsNone(db.get_full_transcript_cache(self.conn, self.video, "groq", "whisper-large-v3-turbo"))

    def test_miss_after_the_video_itself_changed(self):
        # Same path, different release. Every timestamp in the cached transcript belongs to a
        # different cut of the film.
        self.video.write_bytes(b"y" * 9999)
        os.utime(self.video, (time.time() + 600, time.time() + 600))
        self.assertIsNone(db.get_full_transcript_cache(self.conn, self.video, "groq", "whisper-large-v3"))

    def test_rows_from_before_these_columns_existed_stay_usable(self):
        self.conn.execute("UPDATE video_full_transcript_cache SET stt_model = NULL, "
                          "video_mtime = NULL, video_size = NULL")
        self.assertIsNotNone(db.get_full_transcript_cache(self.conn, self.video, "groq", "whisper-large-v3"))

    def test_pruning_is_by_age(self):
        self.conn.execute("UPDATE video_full_transcript_cache SET created_at = ?",
                          ((datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),))
        self.assertEqual(db.prune_full_transcript_cache(self.conn, max_age_days=90), 1)


class GenerateAttempts(TempAppCase):
    def _since(self, hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def test_a_recent_failure_is_inside_its_cooldown(self):
        db.record_generate_attempt(self.conn, self.video, "da", ok=False, error="boom")
        self.assertTrue(db.generate_failed_since(self.conn, self.video, "da", self._since(24)))

    def test_a_later_success_clears_the_cooldown(self):
        db.record_generate_attempt(self.conn, self.video, "da", ok=False, error="boom")
        db.record_generate_attempt(self.conn, self.video, "da", ok=True)
        self.assertFalse(db.generate_failed_since(self.conn, self.video, "da", self._since(24)))

    def test_failures_do_not_consume_the_daily_cap(self):
        # They are already held off by their own cooldown. Counting them too would let three
        # broken videos lock the whole library out of generation for a day.
        db.record_generate_attempt(self.conn, self.video, "da", ok=False, error="boom")
        self.assertEqual(db.count_generated_videos_since(self.conn, self._since(24)), 0)

    def test_extra_languages_of_one_video_count_once(self):
        # Transcription cost is per video; translating it again is comparatively cheap, which is
        # what the cap is protecting.
        db.record_generate_attempt(self.conn, self.video, "da", ok=True)
        db.record_generate_attempt(self.conn, self.video, "sv", ok=True)
        self.assertEqual(db.count_generated_videos_since(self.conn, self._since(24)), 1)

    def test_the_window_is_rolling(self):
        db.record_generate_attempt(self.conn, self.video, "da", ok=True)
        self.conn.execute("UPDATE generate_attempts SET attempted_at = ?", (self._since(48),))
        self.assertEqual(db.count_generated_videos_since(self.conn, self._since(24)), 0)


class WriteNewSubtitle(TempAppCase):
    def setUp(self):
        super().setUp()
        self.subs = _subs(0)

    def test_writes_the_name_discovery_expects(self):
        dest = fileops.write_new_subtitle(self.subs, self.video, "da")
        self.assertEqual(dest.name, "Ocean's Eleven (2001).da.srt")
        from verifyarr.discovery import parse_lang_from_filename
        self.assertEqual(parse_lang_from_filename(dest), "da")

    def test_never_overwrites_an_existing_file(self):
        fileops.write_new_subtitle(self.subs, self.video, "da")
        with self.assertRaises(fileops.SubtitleAlreadyExists):
            fileops.write_new_subtitle(self.subs, self.video, "da")

    def test_no_temp_file_is_left_behind_on_either_path(self):
        fileops.write_new_subtitle(self.subs, self.video, "da")
        with self.assertRaises(fileops.SubtitleAlreadyExists):
            fileops.write_new_subtitle(self.subs, self.video, "da")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_a_file_appearing_between_the_check_and_the_write_still_loses(self):
        # The realistic race: Bazarr's poll downloads into this exact path while we transcribe.
        real_save = pysubs2.SSAFile.save

        def save_then_race(inner_self, path, *args, **kwargs):
            real_save(inner_self, path, *args, **kwargs)
            self.video.with_name(f"{self.video.stem}.da.srt").write_text("1\n", encoding="utf-8")

        with mock.patch.object(pysubs2.SSAFile, "save", save_then_race):
            with self.assertRaises(fileops.SubtitleAlreadyExists):
                fileops.write_new_subtitle(self.subs, self.video, "da")
        self.assertEqual(self.video.with_name(f"{self.video.stem}.da.srt").read_text(), "1\n")

    def test_rejects_anything_that_is_not_a_language_code(self):
        for bad in ("../evil", "a/b", "..", "da.srt.bak", "english", "", "  "):
            with self.assertRaises(ValueError, msg=bad):
                fileops.write_new_subtitle(self.subs, self.video, bad)

    def test_normalizes_case(self):
        self.assertEqual(fileops.write_new_subtitle(self.subs, self.video, "DA").name,
                         "Ocean's Eleven (2001).da.srt")


if __name__ == "__main__":
    unittest.main()
