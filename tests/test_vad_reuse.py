"""speech_timeline: reuse alass' WAV, never remember a failure."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import sync_engine, vad


class SpeechTimelineTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        self.video = d / "ep.mkv"
        self.video.write_bytes(b"x")
        self.wav = d / "ep.wav"
        self.wav.write_bytes(b"RIFF")
        binary, model = d / "vad", d / "m.bin"
        binary.write_bytes(b""), model.write_bytes(b"")
        self.cfg = SimpleNamespace(vad_binary=str(binary), vad_model=str(model))
        vad._VAD_MEMO.clear()
        sync_engine.KNOWN_WAVS.clear()

    def tearDown(self):
        self.td.cleanup()
        vad._VAD_MEMO.clear()
        sync_engine.KNOWN_WAVS.clear()

    def test_alass_wav_is_reused(self):
        sync_engine.KNOWN_WAVS[str(self.video)] = self.wav
        with mock.patch.object(vad, "run_vad_timeline", return_value=[(0.0, 1.0)]) as run, \
                mock.patch.object(sync_engine, "extract_audio_wav") as ext:
            self.assertEqual(vad.speech_timeline(self.video, self.cfg), [(0.0, 1.0)])
        ext.assert_not_called()
        self.assertEqual(Path(run.call_args[0][0]), self.wav)

    def test_wav_older_than_video_is_ignored(self):
        import os
        os.utime(self.wav, ns=(1, 1))
        sync_engine.KNOWN_WAVS[str(self.video)] = self.wav
        with mock.patch.object(vad, "run_vad_timeline", return_value=[(0.0, 1.0)]) as run, \
                mock.patch.object(sync_engine, "extract_audio_wav", return_value=False):
            vad.speech_timeline(self.video, self.cfg)
        self.assertFalse(any(Path(c[0][0]) == self.wav for c in run.call_args_list))

    def test_failure_is_not_remembered(self):
        with mock.patch.object(sync_engine, "extract_audio_wav", return_value=False):
            self.assertIsNone(vad.speech_timeline(self.video, self.cfg))
        sync_engine.KNOWN_WAVS[str(self.video)] = self.wav
        with mock.patch.object(vad, "run_vad_timeline", return_value=[(2.0, 3.0)]):
            self.assertEqual(vad.speech_timeline(self.video, self.cfg), [(2.0, 3.0)])

    def test_resolve_registers_the_wav(self):
        cache = {self.video: self.wav}
        sync_engine.resolve_alass_reference(self.video, cache, Path(self.td.name))
        self.assertEqual(sync_engine.KNOWN_WAVS.get(str(self.video)), self.wav)


if __name__ == "__main__":
    unittest.main()
