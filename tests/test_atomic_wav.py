"""Finding 2: extract_audio_wav must write atomically and reject a truncated WAV.

ffmpeg used to write straight to out_path with -y: on timeout/OSError we returned
False but left the partial file behind, and on exit 0 we only checked that the file
existed and was non-empty -- a truncated WAV (full disk) passed that test. The fix
writes to a tmp file + os.replace and validates the WAV structure afterwards.

subprocess is mocked: the fake writes what ffmpeg would have written (partial, whole
or truncated) to the command's last argument -- out_path on the old code, the tmp
file on the new code. No ffmpeg needed.
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
    """Minimal gyldig 16kHz mono 16-bit PCM WAV, same shape as ffmpeg writes."""
    data = b"\x00\x00" * n_frames
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    riff_size = 4 + 8 + 16 + 8 + len(data)
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


def _fake_run(payload: bytes, returncode=0, exc=None):
    def fake(cmd, **kw):
        if str(cmd[-1]).endswith(".part"):  # not the ionice probe's trailing "true"
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
            self.assertFalse(out.exists(), "partial file left behind on exit != 0")
            self.assertEqual(list(Path(td).glob("*.part")), [])

    def test_timeout_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(b"RIFF-partial",
                                             exc=subprocess.TimeoutExpired("ffmpeg", 1))):
                self.assertFalse(sync_engine.extract_audio_wav(video, out))
            self.assertFalse(out.exists(), "partial file left behind on timeout")
            self.assertEqual(list(Path(td).glob("*.part")), [])

    def test_zero_exit_truncated_wav_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            video, out = self._paths(td)
            with mock.patch.object(sync_engine.subprocess, "run",
                                   _fake_run(wav_bytes()[:100], returncode=0)):
                self.assertFalse(sync_engine.extract_audio_wav(video, out),
                                 "truncated WAV accepted on exit 0")
            self.assertFalse(out.exists(), "truncated WAV left behind as valid output")

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
