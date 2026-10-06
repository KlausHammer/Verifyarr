"""Restoring from quarantine/backups goes back to the media folder the file came from."""
import dataclasses
import tempfile
import unittest
from pathlib import Path

from verifyarr import db
from verifyarr.settings import Config
from verifyarr.web.routers.quarantine import _resolve_media_root


class RestoreRoot(unittest.TestCase):
    def test_picks_the_folder_the_file_came_from(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            movies, tv = td / "movies", td / "tv"
            (tv / "Earl" / "Season 2").mkdir(parents=True)
            movies.mkdir()
            conn = db.connect(td / "t.db")
            cfg = dataclasses.replace(Config.from_db(conn), movies_folder=movies, series_folder=tv)
            self.assertEqual(_resolve_media_root(cfg, None, "Earl/Season 2/x.en.srt", conn), tv)
            self.assertEqual(_resolve_media_root(cfg, str(movies), "Earl/Season 2/x.en.srt", conn), movies)
            conn.close()


if __name__ == "__main__":
    unittest.main()
