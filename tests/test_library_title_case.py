"""One series, two spellings ("Billions" from Bazarr, "BILLIONS" from a folder): one entry."""
import tempfile
import unittest
from pathlib import Path

from verifyarr import db
from verifyarr.web.routers.library import grouped_response


def _row(path, title, se):
    return {"video_path": path, "media_root": "/s", "kind": "series", "title": title,
            "season_episode": se, "has_subtitle": True}


class TitleCase(unittest.TestCase):
    def test_same_title_in_two_cases_is_one_group(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            db.replace_library_videos(conn, [_row("/s/a/e1.mkv", "BILLIONS", "S01E01"),
                                             _row("/s/b/e2.mkv", "Billions", "S03E01")])
            resp = grouped_response(conn, "series")
            conn.close()
        self.assertEqual(resp["total"], 1)
        self.assertEqual(resp["items"][0]["title"], "Billions")
        self.assertEqual({s["season"] for s in resp["items"][0]["seasons"]}, {"S01", "S03"})


if __name__ == "__main__":
    unittest.main()
