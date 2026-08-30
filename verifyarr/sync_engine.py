"""alass — the sync engine itself, plus the audio caching that speeds up a sweep."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

SHIFT_BLOCK_RE = re.compile(r"shifted block of \d+ subtitles with length [\d:.]+ by (-?)([\d:.]+)")


def resolve_alass_bin() -> Optional[str]:
    """alass is baked into the Docker image (see Dockerfile), not a setting. Tries both known
    binary names in case a future image uses one or the other."""
    for candidate in ("alass", "alass-cli"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def extract_audio_wav(video_path: Path, out_path: Path, timeout: int = 180) -> bool:
    """Extracts the full audio track as 16kHz mono WAV — used to cache alass' expensive
    audio decoding across multiple subtitle files for the same video (see audio_cache in
    process_pair/cmd_sweep). alass-cli accepts a WAV just as well as a video file as its
    reference and gives an identical result, but much faster once audio is already decoded
    (~16x in practice) — the benefit disappears if it's only used once, though."""
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def parse_alass_shift_blocks(stderr_text: str) -> list[float]:
    """Extracts the signed shift (in seconds) for each block alass found necessary, from its
    stderr log. A free diagnostic signal — no extra call needed. An episode that's simply out
    of sync for a physically explainable reason (e.g. wrong framerate) typically produces ONE
    block with one even shift. Multiple blocks with inconsistent shifts can be a sign that
    alass is forcing mismatched subtitle content to fit locally in pieces — not proof on its
    own of a wrong subtitle (real cuts/scene changes also produce multiple blocks), but a
    useful extra signal in the report. See `correctness_check` for the primary (word-based)
    check."""
    shifts = []
    for sign, magnitude in SHIFT_BLOCK_RE.findall(stderr_text or ""):
        h, m, s = magnitude.split(":")
        seconds = int(h) * 3600 + int(m) * 60 + float(s)
        shifts.append(-seconds if sign == "-" else seconds)
    return shifts


def run_alass(alass_bin: str, reference_path: Path, subtitle_path: Path, out_path: Path,
              split_penalty: int, timeout: int = 900):
    """reference_path is normally video_path, but can be a pre-extracted WAV (see
    extract_audio_wav) — alass-cli treats both the same."""
    cmd = [alass_bin, str(reference_path), str(subtitle_path), str(out_path),
           "--split-penalty", str(split_penalty)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout", ""
    except OSError as e:
        return False, f"could not run alass ({e})", ""
    # alass-cli writes everything — progress bars AND the "shifted block" summary — to
    # stdout, not stderr (stderr is empty even on success). Error messages can land in
    # either, so combine both here so parse_alass_shift_blocks has something to find.
    output_tail = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode != 0:
        return False, f"alass failed (code {proc.returncode})", output_tail
    if not out_path.exists() or out_path.stat().st_size == 0:
        return False, "alass produced empty output", output_tail
    return True, "ok", output_tail


def resolve_alass_reference(video_path: Path, audio_cache: Optional[dict],
                             audio_cache_dir: Optional[Path],
                             lock: Optional[threading.Lock] = None) -> Path:
    """Finds what alass should use as its reference — a cached WAV if possible (see
    extract_audio_wav), otherwise video_path directly. audio_cache is a dict that lives for
    the whole sweep (video_path -> WAV path, or None if extraction failed/was skipped), so
    multiple subtitle files for the same video only pay for audio decoding once.

    lock: pass one when audio_cache may be touched from more than one thread at once (see
    jobs._run_sweep's parallel sync phase) — without it, two subtitle languages for the same
    video could both see "not cached yet" at once and race to extract/overwrite the same WAV
    path. A plain sequential caller (CLI, single-file jobs) passes None, same as before."""
    if audio_cache is None or audio_cache_dir is None:
        return video_path
    with lock if lock is not None else nullcontext():
        if video_path not in audio_cache:
            digest = hashlib.md5(str(video_path).encode()).hexdigest()[:12]
            wav_path = audio_cache_dir / f"{video_path.stem}.{digest}.wav"
            audio_cache[video_path] = wav_path if extract_audio_wav(video_path, wav_path) else None
        return audio_cache[video_path] or video_path
