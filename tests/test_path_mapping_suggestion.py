"""The path mapping suggested when Bazarr and Verifyarr mount the media under different names."""
import unittest

from verifyarr.bazarr import suggest_mapping as _suggest_mapping


class Suggest(unittest.TestCase):
    def test_different_mount_names(self):
        self.assertEqual(
            _suggest_mapping("/media/tv/Billions/Season 3/B S03E01.mkv", "/tv/Billions/Season 3/B S03E01.mkv"),
            {"local": "/media/tv", "bazarr": "/tv"})

    def test_same_path_needs_nothing(self):
        self.assertIsNone(_suggest_mapping("/media/tv/a.mkv", "/media/tv/a.mkv"))

    def test_nothing_in_common(self):
        self.assertIsNone(_suggest_mapping("/a/b.mkv", "/c/d.mkv"))


if __name__ == "__main__":
    unittest.main()
