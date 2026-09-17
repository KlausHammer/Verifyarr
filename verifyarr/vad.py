"""Speech-activity (VAD) timeline traffic control -- which audio is worth Whisper's time.

A VAD timeline says where speech IS, in seconds, in every language, without
understanding a word -- which is exactly what sample placement needs and exactly
what the correctness/line-order evidence must NOT depend on (a Danish subtitle on
English audio still needs its clips placed on English speech; the word matching
itself is unchanged and stays language-gated where it already is).

Sources, cheapest first (all language-independent):
  1. Transcript segments already cached for this video (free -- a prior check of any
     subtitle, or the full-transcript cache, already mapped the speech). This is the
     production default and needs no new binary, model, or language list.
  2. A VAD TSV sidecar (the whisper_gpu_staging .vad.tsv format) -- parsing support
     for offline evaluation and future sidecars.
  3. A Silero VAD binary run (whisper-vad-speech-segments) when vad_binary/vad_model
     are configured (local-only). The model file is NOT auto-downloaded: unlike the
     Whisper models there is no verified stable URL for it, so it must be mounted or
     placed at vad_model (default /app/models/ggml-silero-v5.1.2.bin).

Deliberately NOT done: skipping silent stretches of a FULL transcription. Full-track
timestamps must stay on the audio's own clock -- compressing out silence is the same
timeline corruption the --vad transcription flag was rejected for (see the VAD A/B
result: agreement 0.91 -> 0.09). Savings come from the sampled path instead: silent
slots are recorded as non-evidence WITHOUT an STT call (same {"start", "error"}
shape extraction failures already produce), which also keeps silence hallucinations
("Thank you", "We'll be right back" over credits) out of the shared cache.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("verifyarr")

# Below this much speech inside a clip_seconds window, the slot is silence for
# sampling purposes: record non-evidence, don't spend an STT call on it.
MIN_SPEECH_SECONDS_DEFAULT = 2.0
# Candidate nudge radius around the dialogue-dense start when searching for speech.
NUDGE_SECONDS = (0.0, 10.0, -10.0, 20.0, -20.0, 30.0, -30.0)
# Nudge granularity when scanning a region without a dialogue-dense anchor.
SCAN_STEP_SECONDS = 5.0

# (video_path, mtime, size) -> [(start, end)] for binary VAD runs this process did.
_VAD_MEMO: dict = {}


def _valid_interval(start: float, end: float) -> Optional[tuple[float, float]]:
    """A (start, end) pair as a timeline interval — or None when degenerate (empty,
    backwards, or negative). One definition of "counts as speech" for every timeline
    source, so the TSV, segment, and binary parsers below can't disagree on it."""
    if end > start >= 0:
        return (start, end)
    return None


def parse_vad_tsv(text: str) -> list[tuple[float, float]]:
    """Parse the whisper_gpu_staging .vad.tsv format (header start_s\\tend_s)."""
    out: list[tuple[float, float]] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            continue  # header line and anything else non-numeric
        interval = _valid_interval(start, end)
        if interval is not None:
            out.append(interval)
    return out


def segments_to_intervals(segments: list[dict]) -> list[tuple[float, float]]:
    """Whisper segments (cached or full-track) as a speech-activity timeline."""
    out: list[tuple[float, float]] = []
    for s in segments or []:
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        interval = _valid_interval(start, end)
        if interval is not None:
            out.append(interval)
    return out


def speech_coverage(intervals: list[tuple[float, float]], lo: float, hi: float) -> float:
    """Seconds of speech inside [lo, hi]. Pure interval math, no audio."""
    total = 0.0
    for start, end in intervals:
        if end <= lo or start >= hi:
            continue
        total += min(end, hi) - max(start, lo)
    return max(0.0, total)


def _window_overlaps_cue(subs, start: float, clip_seconds: float) -> bool:
    """A clip with no subtitle text in it is useless for comparison -- require overlap."""
    end = start + clip_seconds
    for ev in subs.events:
        if ev.start / 1000.0 < end and ev.end / 1000.0 > start:
            return True
    return False


def pick_sample_time(subs, intervals: Optional[list[tuple[float, float]]],
                     region_start: float, region_end: float, clip_seconds: float,
                     base_start: float,
                     min_speech_seconds: float = MIN_SPEECH_SECONDS_DEFAULT,
                     ) -> Optional[float]:
    """Nudge a dialogue-dense clip start onto actual speech.

    base_start (today: pick_dialogue_dense_time) is the cue-overlap guarantee and
    the tie-break: with no timeline (intervals None) or a tie, exactly base_start
    comes back, so behavior without VAD data is bit-for-bit what it was. With a
    timeline, candidate starts around base are scored by speech coverage and the
    best one wins; candidates without cue overlap are rejected. Returns None when
    even the best window holds less than min_speech_seconds of speech -- the caller
    records a non-evidence sample and spends no STT call (see module docstring).
    Deterministic: same inputs, same output, no randomness, no language data.
    """
    if intervals is None:
        return base_start
    best: Optional[float] = None
    best_cov = -1.0
    for nudge in NUDGE_SECONDS:
        cand = base_start + nudge
        if cand < region_start or cand + clip_seconds > region_end + clip_seconds:
            cand = max(region_start, min(cand, region_end))
        if not _window_overlaps_cue(subs, cand, clip_seconds):
            continue
        cov = speech_coverage(intervals, cand, cand + clip_seconds)
        if cov > best_cov + 1e-9:
            best, best_cov = cand, cov
    if best is None or best_cov < min_speech_seconds:
        return None
    return best


def scan_region_time(subs, intervals: list[tuple[float, float]],
                     region_start: float, region_end: float, clip_seconds: float,
                     min_speech_seconds: float = MIN_SPEECH_SECONDS_DEFAULT,
                     ) -> Optional[float]:
    """Fallback placement with no dialogue-dense anchor: densest speech window in the
    region that still overlaps a cue, else None. Same no-STT contract as above."""
    best: Optional[float] = None
    best_cov = min_speech_seconds - 1e-9
    t = region_start
    while t + clip_seconds <= region_end + clip_seconds and t <= region_end:
        if _window_overlaps_cue(subs, t, clip_seconds):
            cov = speech_coverage(intervals, t, t + clip_seconds)
            if cov > best_cov + 1e-9:
                best, best_cov = t, cov
        t += SCAN_STEP_SECONDS
    return best


def timeline_for_video(conn, video_path: Path, cfg) -> Optional[list[tuple[float, float]]]:
    """Best available speech timeline for sample placement, cheapest source first.

    1. Full-transcript cache segments (whole-file map, any provider).
    2. Cached clip segments (offset by their clip starts -- cache stores them
       relative to the clip, see db.get_cached_transcript).
    3. Silero binary run when vad_binary/vad_model are configured.
    None when nothing is available: callers keep today's dialogue-density behavior.
    All sources are language-independent.
    """
    from verifyarr import db
    if conn is not None:
        try:
            full = db.get_full_transcript_cache(conn, video_path)
        except Exception:
            full = None
        if full and full.get("segments"):
            return segments_to_intervals(full["segments"])
        try:
            rows = db.get_cached_transcripts_for_video(conn, video_path)
        except Exception:
            rows = []
        ivs: list[tuple[float, float]] = []
        for r in rows:
            try:
                base = float(r.get("start") or 0.0)
            except (TypeError, ValueError):
                continue
            for s in segments_to_intervals(r.get("segments")):
                ivs.append((s[0] + base, s[1] + base))
        if ivs:
            return ivs
    return run_vad_timeline(video_path, getattr(cfg, "vad_binary", ""),
                            getattr(cfg, "vad_model", ""))


def run_vad_timeline(video_path: Path, binary: str, model: str,
                     threads: int = 4, timeout: float = 600.0,
                     ) -> Optional[list[tuple[float, float]]]:
    """One Silero VAD run over the whole file (local-only). Returns None (never raises)
    when unconfigured, missing, or failed -- callers always fall back to cached
    segments, then to dialogue density. Results are memoized per (path, mtime, size)."""
    if not binary or not model:
        return None
    try:
        st = Path(video_path).stat()
        key = (str(video_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if key in _VAD_MEMO:
        return _VAD_MEMO[key]
    if not Path(binary).is_file() or not Path(model).is_file():
        log.debug("VAD binary/model not present (binary=%r model=%r) -- no timeline",
                  binary, model)
        return None
    cmd = [binary, "-vm", model, "-f", str(video_path), "-t", str(max(1, threads)),
           "-vspd", "100", "-vsd", "50"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("VAD run failed for %s: %s", video_path, e)
        return None
    if proc.returncode != 0:
        log.warning("VAD run failed for %s (exit %s)", video_path, proc.returncode)
        return None
    intervals: list[tuple[float, float]] = []
    for m in re.finditer(r"start\s*=\s*([0-9.]+)\s*,\s*end\s*=\s*([0-9.]+)", proc.stdout):
        try:
            # whisper.cpp prints centiseconds here (see vad_parallel.sh's /100 mapping).
            start, end = float(m.group(1)) / 100.0, float(m.group(2)) / 100.0
        except ValueError:
            continue
        interval = _valid_interval(start, end)
        if interval is not None:
            intervals.append(interval)
    if not intervals and proc.stdout.strip():
        # Zero matches from a non-empty, successful run is ambiguous -- a genuinely silent
        # file, or the binary's output format drifting out from under this regex. Downstream
        # this becomes "confirmed silence" (see pick_sample_time's contract for [] vs None),
        # which is the right call for real silence but would misfire silently on a format
        # change -- surfaced here so that's diagnosable instead of invisible.
        log.warning("VAD run for %s produced output but no parseable start=/end= pairs -- "
                    "treating as confirmed silence; check the binary's output format if that "
                    "seems wrong", video_path)
    _VAD_MEMO[key] = intervals
    return intervals
