"""Subtitle generation is not released: a stored 'on' must not run it, as the UI can no longer switch it off."""
import tempfile
import unittest
from pathlib import Path

from verifyarr import db, settings
from verifyarr.settings import Config


class GenerateUnreleasedTests(unittest.TestCase):
    def _cfg(self):
        tmp = tempfile.mkdtemp()
        conn = db.connect(Path(tmp) / "s.db")
        settings.set_settings_group(conn, "generate", {"enabled": True})
        settings.set_settings_group(conn, "general", {"auto_scan_generate_enabled": True})
        return Config.from_db(conn)

    def test_stored_on_is_ignored(self):
        self.assertFalse(settings.GENERATE_RELEASED)
        cfg = self._cfg()
        self.assertFalse(cfg.generate_enabled)
        self.assertFalse(cfg.auto_scan_generate_enabled)


if __name__ == "__main__":
    unittest.main()
