"""A run's percentage moves with the Whisper chunks finished inside the running files."""
import unittest

from verifyarr import progress


class Inflight(unittest.TestCase):
    def tearDown(self):
        progress.finish("a")
        progress.finish("b")

    def test_fraction_of_a_file(self):
        progress.start("a", 5)
        self.assertEqual(progress.inflight(), 0)
        progress.chunk_done("a")
        progress.chunk_done("a")
        self.assertAlmostEqual(progress.inflight(), 0.4 * progress.TRANSCRIBE_SHARE)
        progress.finish("a")
        self.assertEqual(progress.inflight(), 0)

    def test_two_files_add_up_and_cap_at_one_each(self):
        progress.start("a", 2)
        progress.start("b", 4)
        for _ in range(9):
            progress.chunk_done("a")
        progress.chunk_done("b")
        self.assertAlmostEqual(progress.inflight(), 1.25 * progress.TRANSCRIBE_SHARE)


if __name__ == "__main__":
    unittest.main()
