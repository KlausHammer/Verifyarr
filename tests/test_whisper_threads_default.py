"""WHISPER_THREADS env var, else 10 capped by the usable CPUs."""
import os
import unittest
from unittest import mock

from verifyarr import settings


class WhisperThreadsDefault(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"WHISPER_THREADS": "3"}):
            self.assertEqual(settings._default_whisper_threads(), 3)

    def test_default_is_ten_capped_by_cpus(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHISPER_THREADS", None)
            with mock.patch("os.sched_getaffinity", return_value=set(range(16))):
                self.assertEqual(settings._default_whisper_threads(), 10)
            with mock.patch("os.sched_getaffinity", return_value=set(range(4))):
                self.assertEqual(settings._default_whisper_threads(), 4)

    def test_bad_env_falls_back(self):
        with mock.patch.dict(os.environ, {"WHISPER_THREADS": "abc"}):
            self.assertGreaterEqual(settings._default_whisper_threads(), 1)


if __name__ == "__main__":
    unittest.main()
