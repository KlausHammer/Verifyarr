"""The series drill-down lists seasons in order and episodes 1..N, whatever the title's case."""
import tempfile
import unittest
from pathlib import Path

from verifyarr import db
from verifyarr.web.routers.library import series_episodes


def _row(path, title, se):
    return {"video_path": path, "media_root": "/s", "kind": "series", "title": title,
            "season_episode": se, "has_subtitle": True}


class SeriesEpisodes(unittest.TestCase):
    def test_sorted_and_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.replace_library_videos(conn, [
                _row("/s/x/e10.mkv", "BILLIONS", "S01E10"), _row("/s/x/e2.mkv", "Billions", "S01E02"),
                _row("/s/x/e1.mkv", "Billions", "S01E01"), _row("/s/x/s2.mkv", "Billions", "S02E01"),
                _row("/s/x/extra.mkv", "Billions", None), _row("/s/o/o.mkv", "Other", "S01E01")])
            out = series_episodes(title="billions", user=None, conn=conn)
            conn.close()
        self.assertEqual([s["season"] for s in out["seasons"]][:2], ["S01", "S02"])
        eps = [e["season_episode"] for e in out["seasons"][0]["episodes"]]
        self.assertEqual(eps, ["S01E01", "S01E02", "S01E10"])
        self.assertEqual(sum(len(s["episodes"]) for s in out["seasons"]), 5)
        self.assertEqual(out["seasons"][0]["episodes"][0]["embedded_langs"], [])


if __name__ == "__main__":
    unittest.main()
