"""Whisper screens BEFORE alass: the order, the early exit, and the pre-sync.

These run the real chain (screen_pair -> sync_pair -> correctness_and_finish) against matrix
fixtures. The first framerate build passed seven unit tests while its pipeline path was dead;
an order change is exactly the kind of thing only an end-to-end test can hold down -- including
the harness itself, which called sync_pair directly and would have shown nothing.

Needs the staging tree (/mnt/c/...) like the matrix; skipped elsewhere.
"""
from __future__ import annotations

import copy
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
from verifyarr import db, pipeline
from verifyarr.subtitles import load_subs

MODEL = "tiny.en-greedy-cpu"
STAGING_OK = (M.SWEEP.exists()
                and (M.SWEEP / MODEL / "SH_S01E01.json").exists()
                and (M.SWEEP / MODEL / "SH_S01E02.json").exists()
                and (M.SWEEP / MODEL / "SH_S01E03.json").exists())
_needs_staging = unittest.skipUnless(STAGING_OK, "needs whisper_gpu_staging sweep data")


@_needs_staging
class ScreenOrderTests(unittest.TestCase):
    SLUG = "SH_S01E01"

    def _run(self, subs, mode="sampled", slug=None):
        """Returns (row, resulting subs, alass_calls, screen)."""
        slug = slug or self.SLUG
        fx = M.fixture(slug)
        video = M.media_dir(slug) / fx["video_name"]
        if not video.exists():
            self.skipTest("no video")
        lang, segments = M.audio_evidence(MODEL, slug, fx)
        work = Path(tempfile.mkdtemp(prefix="screen_"))
        conn = db.connect(work / "t.db")
        calls = []
        real_alass = pipeline.run_alass

        def counting_alass(*a, **kw):
            calls.append(a[2] if len(a) > 2 else None)
            return real_alass(*a, **kw)

        captured = {}
        real_screen = pipeline.screen_pair

        def spy_screen(*a, **kw):
            captured["screen"] = real_screen(*a, **kw)
            return captured["screen"]

        pipeline.run_alass = counting_alass
        pipeline.screen_pair = spy_screen
        try:
            cfg = M.cfg_for(conn, mode, "on", groq_model=MODEL)
            tmp = work / "s.srt"
            subs.save(str(tmp))
            with M.patch_whisper_full(lang, segments), M.patch_sampled_transcription(lang, segments):
                row = pipeline.process_pair(video, tmp, "en", cfg, conn)
            return row, load_subs(tmp), calls, captured.get("screen")
        finally:
            pipeline.run_alass = real_alass
            pipeline.screen_pair = real_screen
            conn.close()

    def _orig(self, slug=None):
        slug = slug or self.SLUG
        return M.subs_for(slug, M.fixture(slug))

    def test_healthy_file_ends_at_the_screen_and_alass_never_runs(self):
        """The saving that matters on a slow machine: no alass means no full audio extraction."""
        row, out, calls, screen = self._run(self._orig())
        self.assertEqual(screen["verdict"], "ok", f"screen said {screen}")
        self.assertEqual(calls, [], "alass was run on a file the screen had cleared")
        self.assertEqual(row.get("sync_status"), "already in sync")

    def test_healthy_file_is_not_rewritten(self):
        orig = self._orig()
        _row, out, _c, _s = self._run(copy.deepcopy(orig))
        for a, b in zip(orig.events, out.events):
            self.assertEqual(a.start, b.start)

    def test_stretch_is_corrected_before_alass_sees_it(self):
        """alass alone reads a 2% stretch as a PAL conversion and triples the error. Handed a
        file whose rate is already right it produces one clean block instead."""
        bad = copy.deepcopy(self._orig("SH_S01E02"))
        for e in bad.events:
            e.start, e.end = int(e.start * 1.02), int(e.end * 1.02)
        row, out, calls, screen = self._run(bad, slug="SH_S01E02")
        self.assertEqual(screen["verdict"], "needs_sync")
        self.assertIn("Pre-sync before alass: rate", row.get("note") or "")
        self.assertTrue(calls, "alass should still run after the pre-sync")
        orig = self._orig("SH_S01E02")
        d = [abs(a.start - b.start) / 1000.0 for a, b in zip(orig.events, out.events)]
        within = sum(1 for x in d if x <= 1.0) / len(d)
        self.assertGreater(within, 0.9, f"only {within:.0%} of cues landed within 1s")

    def test_uniform_shift_is_measured_and_pre_applied(self):
        bad = copy.deepcopy(self._orig("SH_S01E02"))
        for e in bad.events:
            e.start, e.end = e.start + 45000, e.end + 45000
        row, out, _calls, screen = self._run(bad, mode="full", slug="SH_S01E02")
        self.assertEqual(screen["verdict"], "needs_sync")
        self.assertIn("Pre-sync before alass: offset", row.get("note") or "")

    def test_block_errors_get_no_pre_sync(self):
        """Blocks are alass' job. A global correction on a block-shaped file is wrong by
        construction, so the screen must hand it over untouched."""
        for seed in (0, 1, 2):
            bad, _k, _d = M.corrupt_piecewise(self._orig(), random.Random(f"blk{seed}"))
            row, _out, calls, screen = self._run(bad)
            self.assertEqual(screen["verdict"], "needs_sync", f"seed {seed}")
            self.assertNotIn("Pre-sync before alass", row.get("note") or "",
                             f"seed {seed}: global correction applied to a block file")
            self.assertTrue(calls, f"seed {seed}: alass never ran on a block file")

    def test_screen_clips_are_reused_not_rebought(self):
        """The whole point of screening first: the clips it buys are cached per video, so the
        repair pass that follows re-reads them instead of paying Whisper again."""
        from verifyarr import correctness
        bad = copy.deepcopy(self._orig())
        for e in bad.events:
            e.start, e.end = e.start + 45000, e.end + 45000
        self._run(bad)
        cost = correctness.whisper_cost.snapshot()
        self.assertGreater(cost.get("cached_audio_s", 0), 0,
                           f"intet genbrugt fra screeningens klip: {cost}")

    def test_cost_is_this_files_bill_not_the_batchs(self):
        """A sweep screens every file in the batch BEFORE any correctness check runs, so a
        reset inside screen_pair wipes the previous file's total: each row then reports the
        last screened file's audio plus everything charged since. Regression guard --
        screen_pair measures a delta and hands it to the row instead."""
        from verifyarr import correctness
        orig = self._orig("SH_S01E03")  # no >=120 s gap: must not escalate
        correctness.whisper_cost.reset()
        correctness.whisper_cost.fresh_s = 999.0     # another file's spend, already charged
        row, _out, _calls, screen = self._run(copy.deepcopy(orig), slug="SH_S01E03")
        self.assertIsNotNone(screen.get("cost"), "screen_pair reported no cost of its own")
        self.assertLess(row["whisper_cost"]["fresh_audio_s"], 900.0,
                        f"this row billed for another file's audio: {row['whisper_cost']}")
        self.assertGreaterEqual(
            row["whisper_cost"]["fresh_audio_s"] + row["whisper_cost"]["cached_audio_s"],
            screen["cost"]["fresh_audio_s"] + screen["cost"]["cached_audio_s"],
            "the screen's own clips fell out of the bill")

    def test_sync_disabled_says_so_even_when_the_screen_cleared_the_file(self):
        """"already in sync" is a claim about the audio. With sync off nothing compared the
        file to the audio on the sync side, so the row must say it was skipped."""
        fx = M.fixture(self.SLUG)
        video = M.media_dir(self.SLUG) / fx["video_name"]
        if not video.exists():
            self.skipTest("no video")
        lang, segments = M.audio_evidence(MODEL, self.SLUG, fx)
        work = Path(tempfile.mkdtemp(prefix="screen_off_"))
        conn = db.connect(work / "t.db")
        try:
            cfg = M.cfg_for(conn, "sampled", "on", groq_model=MODEL, sync_enabled=False)
            tmp = work / "s.srt"
            self._orig().save(str(tmp))
            with M.patch_whisper_full(lang, segments), M.patch_sampled_transcription(lang, segments):
                screen = pipeline.screen_pair(video, tmp, "en", cfg, conn)
                row, _subs = pipeline.sync_pair(video, tmp, "en", cfg, {}, work, screen=screen)
            self.assertEqual(row.get("sync_status"), "skipped (disabled in settings)")
        finally:
            conn.close()

    def test_alass_failure_returns_what_is_on_disk(self):
        """The pre-sync only ever reaches a temp file. If alass fails, the bytes on disk are
        the original -- so the correctness check that follows must be handed those, not the
        pre-synced object it never wrote."""
        fx = M.fixture(self.SLUG)
        video = M.media_dir(self.SLUG) / fx["video_name"]
        if not video.exists():
            self.skipTest("no video")
        lang, segments = M.audio_evidence(MODEL, self.SLUG, fx)
        work = Path(tempfile.mkdtemp(prefix="screen_fail_"))
        conn = db.connect(work / "t.db")
        real_alass = pipeline.run_alass
        pipeline.run_alass = lambda *a, **kw: (False, "boom", "alass exploded")
        try:
            cfg = M.cfg_for(conn, "sampled", "on", groq_model=MODEL)
            bad = copy.deepcopy(self._orig())
            for e in bad.events:            # 2% stretch: the screen WILL pre-sync this
                e.start, e.end = int(e.start * 1.02), int(e.end * 1.02)
            tmp = work / "s.srt"
            bad.save(str(tmp))
            with M.patch_whisper_full(lang, segments), M.patch_sampled_transcription(lang, segments):
                screen = pipeline.screen_pair(video, tmp, "en", cfg, conn)
                row, subs = pipeline.sync_pair(video, tmp, "en", cfg, {}, work, screen=screen)
            self.assertTrue((row.get("sync_status") or "").startswith("error:"))
            on_disk = load_subs(tmp)
            self.assertEqual([e.start for e in subs.events], [e.start for e in on_disk.events],
                             "returned timings are not the ones on disk")
        finally:
            pipeline.run_alass = real_alass
            conn.close()


if __name__ == "__main__":
    unittest.main()
