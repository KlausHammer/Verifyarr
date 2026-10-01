"""Docker-env behavior, no daemon needed: path validation, timezone, PORT, priv-drop."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, fileops, scheduler, settings


class ValidateLocalWhisperModelTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_missing_but_ggml_shaped_is_allowed_for_auto_download(self):
        # The runtime downloads a missing ggml-*.bin on first use (see
        # correctness._download_local_whisper_model), so saving one must not fail.
        self.assertIsNone(
            settings.validate_local_whisper_model(str(self.dir / "ggml-small.bin")))

    def test_existing_readable_model_is_ok(self):
        p = self.dir / "ggml-small.en.bin"
        p.write_bytes(b"0123456789")
        self.assertIsNone(settings.validate_local_whisper_model(str(p)))

    def test_empty_is_rejected(self):
        self.assertIsNotNone(settings.validate_local_whisper_model(""))
        self.assertIsNotNone(settings.validate_local_whisper_model("   "))

    def test_non_ggml_filename_is_rejected(self):
        msg = settings.validate_local_whisper_model(str(self.dir / "small.bin"))
        self.assertIsNotNone(msg)
        self.assertIn("ggml-", msg)

    def test_empty_file_is_rejected(self):
        p = self.dir / "ggml-small.bin"
        p.write_bytes(b"")
        msg = settings.validate_local_whisper_model(str(p))
        self.assertIsNotNone(msg)
        self.assertIn("empty", msg)

    def test_directory_with_ggml_name_is_rejected(self):
        p = self.dir / "ggml-small.bin"
        p.mkdir()
        msg = settings.validate_local_whisper_model(str(p))
        self.assertIsNotNone(msg)

    def test_unreadable_file_is_rejected(self):
        p = self.dir / "ggml-small.bin"
        p.write_bytes(b"0123456789")
        p.chmod(0o000)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("running as root — everything is readable")
            msg = settings.validate_local_whisper_model(str(p))
            self.assertIsNotNone(msg)
            self.assertIn("readable", msg)
        finally:
            p.chmod(0o644)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_missing_in_readonly_dir_is_rejected(self):
        # A :ro bind-mount (e.g. - /models:/models:ro) with a typo'd filename can
        # never be downloaded into — fail at save time, not per audio clip.
        sub = self.dir / "models"
        sub.mkdir()
        try:
            sub.chmod(0o555)
            msg = settings.validate_local_whisper_model(str(sub / "ggml-typo.en.bin"))
            self.assertIsNotNone(msg)
            self.assertIn("not found", msg)
        finally:
            sub.chmod(0o755)


class ValidateExecutableTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_executable_is_ok(self):
        p = self.dir / "whisper-cli"
        p.write_bytes(b"#!/bin/sh\n")
        p.chmod(0o755)
        self.assertIsNone(settings.validate_executable_file(str(p), "binary"))

    def test_missing_is_rejected(self):
        msg = settings.validate_executable_file(str(self.dir / "nope"), "binary")
        self.assertIsNotNone(msg)
        self.assertIn("not found", msg)

    def test_empty_is_rejected(self):
        self.assertIsNotNone(settings.validate_executable_file("", "binary"))

    def test_non_executable_is_rejected(self):
        p = self.dir / "whisper-cli"
        p.write_bytes(b"#!/bin/sh\n")
        p.chmod(0o644)
        msg = settings.validate_executable_file(str(p), "binary")
        self.assertIsNotNone(msg)
        self.assertIn("executable", msg)

    def test_directory_is_rejected(self):
        self.assertIsNotNone(
            settings.validate_executable_file(str(self.dir), "binary"))


class SetSettingsGroupValidationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_bad_model_name_blocked_when_local_whisper_on(self):
        with self.assertRaises(ValueError):
            settings.set_settings_group(self.conn, "correctness", {
                "local_whisper_model": "/models/small.bin",
            })

    def test_missing_binary_blocked_when_local_whisper_on(self):
        with self.assertRaises(ValueError):
            settings.set_settings_group(self.conn, "correctness", {
                "local_whisper_binary": "/nonexistent/whisper-cli",
            })

    def test_partial_update_without_paths_saves(self):
        # Partial updates (wizard, automation tab) only carry their own keys.
        settings.set_settings_group(self.conn, "correctness", {"require_audio_lang": "en"})

    def test_bad_log_level_blocked(self):
        with self.assertRaises(ValueError):
            settings.set_settings_group(self.conn, "log", {"level": "nope"})
        with self.assertRaises(ValueError):
            settings.set_settings_group(self.conn, "log", {"level": "debug"})  # case matters

    def test_good_log_level_passes(self):
        settings.set_settings_group(self.conn, "log", {"level": "DEBUG"})
        self.assertEqual(
            settings.get_settings_group(self.conn, "log", redact_secrets=False)["level"],
            "DEBUG")


class ServerTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get("TZ")
        if hasattr(time, "tzset"):
            self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def _set_tz(self, name):
        if not hasattr(time, "tzset"):
            self.skipTest("needs time.tzset")
        os.environ["TZ"] = name
        time.tzset()
        try:  # tzlocal caches the zone it first saw; apscheduler builds its triggers from it
            import tzlocal
            tzlocal.reload_localzone()
        except Exception:
            pass

    def test_server_timezone_name_prefers_tz_env(self):
        self._set_tz("Europe/Copenhagen")
        self.assertEqual(scheduler.server_timezone_name(), "Europe/Copenhagen")

    def test_next_sweep_at_fires_at_local_wall_time(self):
        # "0 4 * * 0" fires Sunday 04:00 Copenhagen wall time (CET = UTC+1).
        self._set_tz("Europe/Copenhagen")
        now = datetime(2026, 1, 5, 12, 0, 0)
        iso = scheduler.next_sweep_at("0 4 * * 0", now=now)
        self.assertIsNotNone(iso)
        fire = datetime.fromisoformat(iso)
        self.assertIsNotNone(fire.tzinfo, "must carry an offset for the browser")
        self.assertEqual((fire.hour, fire.minute), (4, 0))
        self.assertEqual(fire.weekday(), 6)  # Sunday
        self.assertEqual(fire.utcoffset().total_seconds(), 3600)

    def test_next_sweep_at_invalid_cron_is_none(self):
        self.assertIsNone(scheduler.next_sweep_at("not a cron"))


class WriteNewSubtitleOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_new_subtitle_inherits_video_permissions(self):
        pysubs2 = pytest.importorskip("pysubs2")
        video = self.dir / "movie.mkv"
        video.write_bytes(b"fake")
        video.chmod(0o640)
        subs = pysubs2.SSAFile()
        dest = fileops.write_new_subtitle(subs, video, "en")
        mode = stat.S_IMODE(dest.stat().st_mode)
        self.assertEqual(mode, 0o640)


class PortFromEnvTests(unittest.TestCase):
    def test_default_and_valid(self):
        from verifyarr.web.__main__ import port_from_env
        old = os.environ.get("PORT")
        try:
            os.environ.pop("PORT", None)
            self.assertEqual(port_from_env(), 6868)
            os.environ["PORT"] = "9999"
            self.assertEqual(port_from_env(), 9999)
        finally:
            if old is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = old

    def test_invalid_falls_back_to_default(self):
        from verifyarr.web.__main__ import port_from_env
        old = os.environ.get("PORT")
        try:
            os.environ["PORT"] = "notaport"
            self.assertEqual(port_from_env(), 6868)
        finally:
            if old is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = old


class LogLevelParsingTests(unittest.TestCase):
    def test_known_levels_pass_through_case_insensitively(self):
        from verifyarr import _parse_log_level
        self.assertEqual(_parse_log_level("debug"), "DEBUG")
        self.assertEqual(_parse_log_level("  info "), "INFO")
        self.assertEqual(_parse_log_level("WARNING"), "WARNING")

    def test_unknown_levels_fall_back_to_info(self):
        from verifyarr import _parse_log_level
        self.assertEqual(_parse_log_level("nope"), "INFO")
        self.assertEqual(_parse_log_level(""), "INFO")


class CliPrivilegeDropTests(unittest.TestCase):
    def _run_drop(self, euid, env):
        from unittest import mock
        from verifyarr import cli
        old = {k: os.environ.get(k) for k in ("PUID", "PGID")}
        try:
            for k in ("PUID", "PGID"):
                os.environ.pop(k, None)
            os.environ.update(env)
            with mock.patch("os.geteuid", return_value=euid), \
                    mock.patch("os.setgid") as setgid, \
                    mock.patch("os.setuid") as setuid:
                manager = mock.Mock()
                manager.attach_mock(setgid, "setgid")
                manager.attach_mock(setuid, "setuid")
                cli._drop_root_to_puid()
                return manager.mock_calls
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_root_with_puid_drops_gid_first(self):
        calls = self._run_drop(0, {"PUID": "1000", "PGID": "1005"})
        self.assertEqual([c[0] for c in calls], ["setgid", "setuid"])
        self.assertEqual(calls[0][1], (1005,))
        self.assertEqual(calls[1][1], (1000,))

    def test_non_root_never_drops(self):
        self.assertEqual(self._run_drop(1000, {"PUID": "1000"}), [])

    def test_root_without_puid_stays_root(self):
        self.assertEqual(self._run_drop(0, {}), [])
        self.assertEqual(self._run_drop(0, {"PUID": "0"}), [])
        self.assertEqual(self._run_drop(0, {"PUID": "notanid"}), [])


class MatchReferenceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_exec_bits_are_not_copied(self):
        # Videos on SMB/NTFS are often 0777; the subtitle must not gain exec bits.
        ref = self.dir / "movie.mkv"
        ref.write_bytes(b"fake")
        ref.chmod(0o777)
        dest = self.dir / "movie.en.srt"
        dest.write_bytes(b"1\n")
        fileops.match_reference_ownership(dest, ref)
        mode = stat.S_IMODE(dest.stat().st_mode)
        self.assertEqual(mode & 0o111, 0)
        self.assertEqual(mode, 0o666)

    def test_missing_reference_is_quiet(self):
        dest = self.dir / "movie.en.srt"
        dest.write_bytes(b"1\n")
        fileops.match_reference_ownership(dest, self.dir / "nope.mkv")


class PutSettingsGroupHttpTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_bad_model_returns_422_not_500(self):
        from fastapi import HTTPException
        from verifyarr.web.routers import settings as settings_router
        body = settings_router.SettingsGroupBody(values={"local_whisper_model": "/models/small.bin"})
        with self.assertRaises(HTTPException) as ctx:
            settings_router.put_group("correctness", body, user=None, conn=self.conn)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("ggml-", str(ctx.exception.detail))

    def test_good_save_returns_group(self):
        from verifyarr.web.routers import settings as settings_router
        body = settings_router.SettingsGroupBody(values={"local_whisper_threads": 2})
        saved = settings_router.put_group("correctness", body, user=None, conn=self.conn)
        self.assertEqual(saved["local_whisper_threads"], 2)
        self.assertNotIn("stt_provider", saved)


class NextRunEndpointTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_next_run_carries_timezone(self):
        from verifyarr.web.routers import runs as runs_router
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Copenhagen"
            if hasattr(time, "tzset"):
                time.tzset()
            result = runs_router.next_run(user=None, conn=self.conn)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            if hasattr(time, "tzset"):
                time.tzset()
        self.assertIn("next_run_at", result)
        self.assertEqual(result["timezone"], "Europe/Copenhagen")
