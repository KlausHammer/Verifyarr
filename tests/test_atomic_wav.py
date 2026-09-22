"""Fund 2: extract_audio_wav skal skrive atomart og afvise en afkortet WAV.

ffmpeg skriver i dag direkte til out_path med -y: ved timeout/OSError returnerer vi
False men lader den delvise fil ligge, og ved exit 0 tjekker vi kun at filen findes
og er ikke-tom -- en afkortet WAV (fuld disk) bestaar den test. Rettelsen skriver
til tmp + os.replace og validerer WAV-strukturen bagefter.

subprocess er mock'et: fake'en skriver det ffmpeg ville have skrevet (delvist, helt
eller afkortet) til kommandoens sidste argument -- paa gammel kode er det out_path
selv, paa ny kode tmp-filen. Ingen ffmpeg-noedvendighed.
"""
from __future__ import annotations

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from verifyarr import sync_engine


def wav_bytes(n_frames=1600, rate=16000):
    """Minimal gyldig 16kHz mono 16-bit PCM WAV, samme form som ffmpeg skriver."""
    data = b"\x00\x00" * n_frames
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    riff_size = 4 + 8 + 16 + 8 + len(data)
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


def _fake_run(payload: bytes, returncode=0, exc=None):
    def fake(cmd, **kw):
        Path(cmd[-1]).write_bytes(payload)
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")
    return fake


class WavCompleteTests(unittest.TestCase):
    def test_valid_wav_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ok.wav"
            p.write_bytes(wav_bytes())
            self.assertTrue(sync_engine.wav_complete(p))

    def test_truncated_wav_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cut.wav"
            p.write_bytes(wav_bytes()[:100])  # headeren lover mere end filen har
            self.assertFalse(sync_engine.wav_complete(p))

    def test_garbage_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "junk.wav"
            p.write_bytes(b"fake-wav")
            self.assertFalse(sync_engine.wav_complete(p))

    def test_missing_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(sync_engine.wav_complete(Path(td) / "nope.wav"))


class AtomicExtractTests(unittest.TestCase):
    def _paths(self, td):
        return Path(td) / "ep.mkv", Path(td) / "ep.wav"

    def test_failed_ffmpeg_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(b"RIFF-partial", returncode=1)):
                self.assertFalse(sync_engine.extract_audio_wav(video, out))
            self.assertFalse(out.exists(), "delvis fil efterladt ved exit != 0")
            self.assertEqual(list(Path(td).glob("*.part")), [])

    def test_timeout_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(b"RIFF-partial",
                                             exc=subprocess.TimeoutExpired("ffmpeg", 1))):
                self.assertFalse(sync_engine.extract_audio_wav(video, out))
            self.assertFalse(out.exists(), "delvis fil efterladt ved timeout")
            self.assertEqual(list(Path(td).glob("*.part")), [])

    def test_zero_exit_truncated_wav_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(wav_bytes()[:100], returncode=0)):
                self.assertFalse(sync_engine.extract_audio_wav(video, out),
                                 "afkortet WAV accepteret ved exit 0")
            self.assertFalse(out.exists(), "afkortet WAV efterladt som gyldigt output")

    def test_success_writes_valid_file_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(wav_bytes(), returncode=0)):
                self.assertTrue(sync_engine.extract_audio_wav(video, out))
            self.assertTrue(sync_engine.wav_complete(out))
            self.assertEqual(list(Path(td).glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
