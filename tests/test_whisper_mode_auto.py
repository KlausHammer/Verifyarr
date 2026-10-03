"""sync.whisper_mode=auto: full with a tiny model, sampled otherwise."""
import unittest

from verifyarr import settings


class WhisperModeAuto(unittest.TestCase):
    def test_auto_picks_by_model(self):
        r = settings.resolve_whisper_mode
        self.assertEqual(r("auto", "/m/ggml-tiny.en.bin"), "full")
        self.assertEqual(r("auto", "/m/ggml-tiny.en-q5_1.bin"), "full")
        self.assertEqual(r("auto", "/m/ggml-small.en.bin"), "sampled")
        self.assertEqual(r("auto", "/m/ggml-medium.en.bin"), "sampled")

    def test_explicit_wins(self):
        self.assertEqual(settings.resolve_whisper_mode("sampled", "/m/ggml-tiny.en.bin"), "sampled")
        self.assertEqual(settings.resolve_whisper_mode("full", "/m/ggml-small.en.bin"), "full")

    def test_default_is_auto(self):
        self.assertEqual(settings.SETTING_DEFS["sync.whisper_mode"][2], "auto")


if __name__ == "__main__":
    unittest.main()
