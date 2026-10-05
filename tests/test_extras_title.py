"""Discs and extras belong to the title above them, not to a series of their own."""
import unittest
from pathlib import Path

from verifyarr.discovery import infer_title_and_episode

SHOWS = Path("/media/shows")


class ExtrasTitle(unittest.TestCase):
    def title(self, rel, root=SHOWS):
        return infer_title_and_episode(root / rel, root, root == SHOWS)[1]

    def test_disk_folders_are_not_series(self):
        for n in (1, 2, 3):
            self.assertEqual(self.title(f"Some Show/Disk {n}/video.mkv"), "Some Show")

    def test_any_name_below_a_show_folder_belongs_to_it(self):
        self.assertEqual(self.title("Some Show/Whatever Name/clip.mkv"), "Some Show")

    def test_deep_extras_belong_to_the_show(self):
        self.assertEqual(self.title("Some Show/Featurettes/Season 1/Deleted Scenes/Disk 1.mkv"), "Some Show")

    def test_movies(self):
        movies = Path("/media/movies")
        self.assertEqual(self.title("Inception (2010)/Inception.mkv", movies), "Inception (2010)")
        self.assertEqual(self.title("Collection/Movie 1 (2010)/m.mkv", movies), "Movie 1 (2010)")
        self.assertEqual(self.title("Inception (2010)/Extras/Making of.mkv", movies), "Extras")


if __name__ == "__main__":
    unittest.main()
