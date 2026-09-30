"""The English audio track is what sync and checks listen to, not a dub."""
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from verifyarr import audiotrack, correctness, sync_engine


def _probe(*streams):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": list(streams)}), stderr="")


def _stream(lang=None, title=None):
    tags = {}
    if lang:
        tags["language"] = lang
    if title:
        tags["title"] = title
    return {"index": 1, "tags": tags}


class EnglishAudioIndexTests(unittest.TestCase):
    def setUp(self):
        audiotrack._MEMO.clear()

    def idx(self, *streams):
        with mock.patch.object(audiotrack.subprocess, "run", return_value=_probe(*streams)):
            return audiotrack.english_audio_index(Path("/nonexistent/v.mkv"))

    def test_english_second_track(self):
        self.assertEqual(self.idx(_stream("fre"), _stream("eng")), 1)

    def test_english_first_track(self):
        self.assertEqual(self.idx(_stream("eng"), _stream("fre")), 0)

    def test_no_english_keeps_default(self):
        self.assertIsNone(self.idx(_stream("dan")))

    def test_untagged_track_titled_english(self):
        self.assertEqual(self.idx(_stream("und", "French"), _stream("und", "English (US)")), 1)

    def test_ffprobe_failure_is_no_answer(self):
        with mock.patch.object(audiotrack.subprocess, "run", side_effect=OSError):
            self.assertIsNone(audiotrack.english_audio_index(Path("/nonexistent/v.mkv")))

    def test_map_args(self):
        with mock.patch.object(audiotrack, "english_audio_index", return_value=1):
            self.assertEqual(audiotrack.audio_map_args(Path("v.mkv")), ["-map", "0:a:1"])
        with mock.patch.object(audiotrack, "english_audio_index", return_value=None):
            self.assertEqual(audiotrack.audio_map_args(Path("v.mkv")), [])


class LanguageAndCommandTests(unittest.TestCase):
    def test_language_is_english_when_a_second_track_is_english(self):
        with mock.patch.object(correctness, "english_audio_index", return_value=1):
            self.assertEqual(correctness.detect_audio_language_ffprobe(Path("v.mkv")), "en")

    def test_language_falls_back_to_first_track_without_english(self):
        data = {"streams": [{"tags": {"language": "dan"}}]}
        with mock.patch.object(correctness, "english_audio_index", return_value=None), \
                mock.patch.object(correctness, "_ffprobe_json", return_value=data):
            self.assertEqual(correctness.detect_audio_language_ffprobe(Path("v.mkv")), "da")

    def _cmd_of(self, fn, *args):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        with mock.patch.object(correctness, "english_audio_index", return_value=1), \
                mock.patch.object(audiotrack, "english_audio_index", return_value=1), \
                mock.patch("subprocess.run", fake_run):
            fn(*args)
        return seen["cmd"]

    def test_clip_and_wav_extraction_map_the_english_track(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.wav"
            clip = self._cmd_of(correctness.extract_clip, Path("v.mkv"), 10.0, 30, out)
            wav = self._cmd_of(sync_engine.extract_audio_wav, Path("v.mkv"), out)
        for cmd in (clip, wav):
            i = cmd.index("-map")
            self.assertEqual(cmd[i + 1], "0:a:1")

    def test_alass_reference_gets_its_own_wav_for_a_later_english_track(self):
        with mock.patch.object(sync_engine, "english_audio_index", return_value=1), \
                mock.patch.object(sync_engine, "extract_audio_wav", return_value=True) as ext:
            ref = sync_engine.resolve_alass_reference(Path("/m/v.mkv"), None, None)
        self.assertTrue(str(ref).endswith(".en.wav"))
        ext.assert_called_once()

    def test_alass_reference_is_the_video_when_english_is_first_or_absent(self):
        for idx in (0, None):
            with mock.patch.object(sync_engine, "english_audio_index", return_value=idx):
                ref = sync_engine.resolve_alass_reference(Path("/m/v.mkv"), None, None)
            self.assertEqual(ref, Path("/m/v.mkv"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg needed")
class RealFileTests(unittest.TestCase):
    """A real mkv: track 1 = French (300 Hz), track 2 = English (900 Hz)."""

    @staticmethod
    def _freq(path: Path) -> float:
        with wave.open(str(path)) as w:
            n = w.getnframes()
            frames = w.readframes(n)
            rate = w.getframerate()
        import array
        a = array.array("h", frames)
        crossings = sum(1 for x, y in zip(a, a[1:]) if x < 0 <= y)
        return crossings / (n / rate)

    def test_english_track_is_the_one_extracted(self):
        audiotrack._MEMO.clear()
        with tempfile.TemporaryDirectory() as td:
            mkv = Path(td) / "v.mkv"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=300:d=3", "-f", "lavfi", "-i", "sine=f=900:d=3",
                 "-map", "0:a", "-map", "1:a", "-metadata:s:a:0", "language=fre", "-metadata:s:a:1", "language=eng",
                 "-c:a", "aac", str(mkv)], capture_output=True, check=True)
            self.assertEqual(audiotrack.english_audio_index(mkv), 1)
            self.assertEqual(correctness.detect_audio_language_ffprobe(mkv), "en")
            out = Path(td) / "clip.wav"
            self.assertTrue(correctness.extract_clip(mkv, 0.5, 2, out))
            self.assertAlmostEqual(self._freq(out), 900, delta=40)
            wav = Path(td) / "full.wav"
            self.assertTrue(sync_engine.extract_audio_wav(mkv, wav))
            self.assertAlmostEqual(self._freq(wav), 900, delta=40)


if __name__ == "__main__":
    unittest.main()
