"""The Bazarr poll runs at the idle pace, and every 3 minutes while a replacement is pending."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verifyarr import bazarr_poll, db
from verifyarr.settings import Config


class PollDue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.cfg = Config.from_db(self.conn)
        self.now = time.time()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _last(self, minutes_ago):
        db.set_setting_raw(self.conn, bazarr_poll._LAST_KEY, str(self.now - minutes_ago * 60))

    def test_idle_waits_for_the_setting(self):
        self._last(10)
        self.assertFalse(bazarr_poll._due(self.conn, self.cfg, self.now))
        self._last(61)
        self.assertTrue(bazarr_poll._due(self.conn, self.cfg, self.now))

    def test_pending_replacement_polls_fast(self):
        db.add_blacklist_action(self.conn, subtitle_path="/x.srt", kind="episode", episode_id="7", language="en")
        self._last(4)
        self.assertTrue(bazarr_poll._due(self.conn, self.cfg, self.now))
        self._last(1)
        self.assertFalse(bazarr_poll._due(self.conn, self.cfg, self.now))

    def test_stops_when_bazarr_no_longer_wants_it(self):
        db.add_blacklist_action(self.conn, subtitle_path="/x.srt", kind="episode", episode_id="7", language="en")
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self.conn.execute("UPDATE blacklist_actions SET blacklisted_at = ?", (old,))
        self.conn.commit()
        self.assertEqual(db.pending_replacements(self.conn, 6)[0]["episode_id"], "7")
        bazarr_poll._resolve_pending(self.conn, "series", {(7, "en")})  # still wanted
        self.assertEqual(len(db.pending_replacements(self.conn, 6)), 1)
        bazarr_poll._resolve_pending(self.conn, "series", set())  # satisfied
        self.assertEqual(db.pending_replacements(self.conn, 6), [])

    def test_first_poll_is_due(self):
        self.assertTrue(bazarr_poll._due(self.conn, self.cfg, self.now))


if __name__ == "__main__":
    unittest.main()
