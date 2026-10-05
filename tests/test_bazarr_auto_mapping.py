"""Bazarr's paths are matched to the library's by shared folders and file name; the mapping is learned."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from verifyarr import bazarr, db
from verifyarr.settings import Config


def _resp(items):
    return SimpleNamespace(status_code=200, json=lambda: {"data": items})


class AutoMapping(unittest.TestCase):
    def test_learns_prefix_from_a_match(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "t.db")
            cfg = Config.from_db(conn)
            conn.close()
        cfg = cfg.__class__(**{**cfg.__dict__, "bazarr_url": "http://b", "bazarr_api_key": "k"}) \
            if False else __import__("dataclasses").replace(cfg, bazarr_url="http://b", bazarr_api_key="k")
        local = [Path("/media/tv/Billions/Season 1/B S01E01.mkv"), Path("/media/tv/Billions/Season 1/B S01E02.mkv")]

        def fake(cfg_, method, path, **kw):
            if path == "/movies":
                return _resp([])
            if path == "/series":
                return _resp([{"sonarrSeriesId": 1, "title": "Billions"}])
            return _resp([{"sonarrSeriesId": 1, "sonarrEpisodeId": 10, "path": "/tv/Billions/Season 1/B S01E01.mkv", "season": 1, "episode": 1},
                          {"sonarrSeriesId": 1, "sonarrEpisodeId": 11, "path": "/tv/Billions/Season 1/B S01E02.mkv", "season": 1, "episode": 2}])

        saved = []
        with mock.patch.object(bazarr, "bazarr_request", fake), \
                mock.patch.object(bazarr, "_save_learned_mapping", lambda c, l: saved.extend(l)):
            ids = {}
            _emb, titles = bazarr.bazarr_library_info(cfg, ids_out=ids, local_videos=local)
        self.assertEqual(titles, {local[0]: "Billions", local[1]: "Billions"})
        self.assertEqual(saved, [("/media/tv", "/tv")])
        self.assertEqual(ids[local[1]]["season_episode"], "S01E02")


if __name__ == "__main__":
    unittest.main()
