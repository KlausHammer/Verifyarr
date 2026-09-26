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

from verifyarr.procprio import wrap_low_priority

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
_VAD_MEMO_MAX = 64  # the webapp lives for weeks; a sweep touches thousands of files


def _memo_put(key, value) -> None:
    if len(_VAD_MEMO) >= _VAD_MEMO_MAX:
        _VAD_MEMO.pop(next(iter(_VAD_MEMO)))
    _VAD_MEMO[key] = value


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
                     whole_file: bool = True) -> Optional[float]:
    """Nudge a dialogue-dense clip start onto actual speech.

    base_start (today: pick_dialogue_dense_time) is the cue-overlap guarantee and
    the tie-break: with no timeline (intervals None) or a tie, exactly base_start
    comes back, so behavior without VAD data is bit-for-bit what it was. With a
    timeline, candidate starts around base are scored by speech coverage and the
    best one wins; candidates without cue overlap are rejected. Returns None when
    even the best window holds less than min_speech_seconds of speech -- the caller
    records a non-evidence sample and spends no STT call (see module docstring).
    Deterministic: same inputs, same output, no randomness, no language data.

    whole_file=False means the timeline only maps what has already been sampled (cached
    clips, see timeline_for_video). Then the nudge still applies -- moving onto known
    speech is safe on partial data -- but a low-coverage window is UNMEASURED, not silent,
    and base_start comes back rather than None. Skipping it would mean never looking at
    the regions we have not looked at.
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
        return None if whole_file else base_start
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


def timeline_for_video(conn, video_path: Path, cfg
                       ) -> tuple[Optional[list[tuple[float, float]]], bool]:
    """(speech intervals, whole_file) -- best available timeline for sample placement.

    1. Full-transcript cache segments (whole-file map, any provider).  whole_file=True
    2. Cached clip segments, offset by their clip starts.               whole_file=False
    3. Silero binary run when vad_binary/vad_model are configured.      whole_file=True
       WAV input only (the binary reads WAV): for a video this source is off, and that
       is deliberate -- decoding it first changed no matrix class on SH (p50 12 better,
       12 worse, sampled), so placement is not worth an extra decode before alass.
    (None, False) when nothing is available: callers keep dialogue-density behavior.

    The second value is the whole point of the pair, and leaving it out was a real bug.
    Sources 1 and 3 map the entire file, so a stretch with no interval in it is silence.
    Source 2 maps only what has already been sampled -- perhaps three clips out of a
    21-minute episode -- so a stretch with no interval in it is simply unmeasured. Read as
    a whole-file map it declares everything we have not looked at to be silent, and the
    caller then skips those regions without spending an STT call: the file is never looked
    at precisely because it was never looked at. Raising sync.sample_count from 3 to 16
    made that bite hard, since 13 of the new regions lie outside any cached clip by
    construction. Partial data is still useful for NUDGING a clip onto known speech; it is
    never evidence of absence. All sources are language-independent."""
    from verifyarr import db
    if conn is not None:
        try:
            full = db.get_full_transcript_cache(conn, video_path)
        except Exception:
            full = None
        if full and full.get("segments"):
            return segments_to_intervals(full["segments"]), True
        try:
            _provider, _model = db.full_transcript_cache_key(cfg)
            rows = db.get_cached_transcripts_for_video(conn, video_path,
                                                       stt_provider=_provider, stt_model=_model)
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
            # Try a real whole-file run first: it can answer "is this silent", which these
            # clips cannot. Only fall back to them when there is no VAD binary configured.
            whole = run_vad_timeline(video_path, getattr(cfg, "vad_binary", ""),
                                     getattr(cfg, "vad_model", ""))
            return (whole, True) if whole else (ivs, False)
    return run_vad_timeline(video_path, getattr(cfg, "vad_binary", ""),
                            getattr(cfg, "vad_model", "")), True


def run_vad_timeline(video_path: Path, binary: str, model: str,
                     threads: int = 4, timeout: float = 600.0, memo: bool = True,
                     ) -> Optional[list[tuple[float, float]]]:
    """One Silero VAD run over the whole file (local-only). Returns None (never raises)
    when unconfigured, missing, or failed -- callers always fall back to cached
    segments, then to dialogue density. Results are memoized per (path, mtime, size)."""
    if not binary or not model:
        return None
    # The binary reads WAV only; on a video it hangs or fails (see speech_timeline).
    if Path(video_path).suffix.lower() != ".wav":
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
        # Low priority like every other heavy call (N100 shares the box).
        proc = subprocess.run(wrap_low_priority(cmd), capture_output=True, text=True,
                              timeout=timeout)
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
    if memo:  # temp WAVs are not memoized: their paths die with the run
        _memo_put(key, intervals)
    return intervals


def speech_timeline(video_path: Path, cfg) -> Optional[list[tuple[float, float]]]:
    """Real VAD speech intervals for the whole file, or None when VAD isn't set up.
    whisper-vad-speech-segments wants WAV, so a video is decoded once to a temp WAV."""
    binary, model = getattr(cfg, "vad_binary", ""), getattr(cfg, "vad_model", "")
    if not binary or not model or not Path(binary).is_file() or not Path(model).is_file():
        return None  # not set up: skip the audio decode too
    try:
        st = Path(video_path).stat()
        key = ("wav", str(video_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if key in _VAD_MEMO:
        return _VAD_MEMO[key]
    from verifyarr import sync_engine
    out = run_vad_timeline(video_path, binary, model) if Path(video_path).suffix.lower() == ".wav" else None
    known = sync_engine.KNOWN_WAVS.get(str(video_path))
    if (not out and known is not None and Path(known).is_file()
            and Path(known).stat().st_mtime_ns >= st.st_mtime_ns):  # not from an older video
        out = run_vad_timeline(known, binary, model)  # alass already decoded it
    if not out:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "audio.wav"
            out = (run_vad_timeline(wav, binary, model, memo=False)
                   if sync_engine.extract_audio_wav(Path(video_path), wav) else None)
    if out is not None:  # a failure is retried next time, not remembered
        _memo_put(key, out)
    return out


def shift_fits_speech(intervals: list[tuple[float, float]], cues: list[tuple[float, float]],
                      shift: float, pad: float = 20.0, step: float = 0.05) -> bool:
    """True when the cues sit on speech better moved by shift than where they are.
    Score = cue time on speech minus cue time on silence (local pattern match)."""
    if not cues:
        return False
    t0 = min(a for a, _ in cues) - pad + min(0.0, shift)
    t1 = max(b for _, b in cues) + pad + max(0.0, shift)
    n = int((t1 - t0) / step)
    if n <= 0:
        return False

    def _mask(spans):
        m = bytearray(n)
        for a, b in spans:
            for k in range(max(0, int((a - t0) / step)), min(n, int((b - t0) / step))):
                m[k] = 1
        return m

    v = _mask([(a, b) for a, b in intervals if b >= t0 and a <= t1])

    def _score(sh):
        c = _mask([(a + sh, b + sh) for a, b in cues])
        return sum((1 if x else -1) for x, y in zip(v, c) if y)

    return _score(shift) > _score(0.0)


def block_witness(intervals: list[tuple[float, float]], cues: list[tuple[float, float]],
                  win: float = 60.0, search: float = 30.0, step: float = 0.1) -> bool:
    """Cheap block suspicion from VAD alone (no Whisper): 60s cue windows, 30s apart,
    each matched against speech at shifts +-30s. Healthy SH gives stray windows at the
    search edge; blocks give consecutive windows agreeing on one shift. Measured (SH,
    tiny): 6/6 silent sampled blocks trigger, 1/6 clean files (a wasted look, no flag).
    True = worth buying the full transcript; the verdict is taken there."""
    if not intervals or not cues:
        return False
    end = max(b for _, b in cues)
    hits, t = [], 0.0
    while t < end:
        cw = [c for c in cues if t <= c[0] < t + win]
        if len(cw) >= 6:
            t0 = t - search - 5.0
            n = int((win + 2 * search + 10.0) / step)
            v = bytearray(n)
            for a, b in intervals:
                if b < t0 or a > t0 + n * step:
                    continue
                for k in range(max(0, int((a - t0) / step)), min(n, int((b - t0) / step))):
                    v[k] = 1

            def _score(sh):
                c = bytearray(n)
                for a, b in cw:
                    for k in range(max(0, int((a + sh - t0) / step)), min(n, int((b + sh - t0) / step))):
                        c[k] = 1
                return sum((1 if x else -1) for x, y in zip(v, c) if y)

            s0 = _score(0.0)
            best_score, best = max((_score(k * 0.5), k * 0.5)
                                   for k in range(int(-search * 2), int(search * 2) + 1))
            total = sum(b - a for a, b in cw) / step
            gain = (best_score - s0) / max(total, 1.0)
            hits.append((t, best if abs(best) >= 2.0 and gain >= 0.25 else None, gain))
        t += win / 2
    for (t1, s1, g1), (t2, s2, _g2) in zip(hits, hits[1:]):
        if s1 is not None and s2 is not None and abs(s1 - s2) <= 1.0 and abs(s1) <= search - 5.0 \
                and t2 - t1 <= win / 2 + 0.1:
            return True
    return any(s is not None and abs(s) <= 10.0 and g >= 0.3 for _, s, g in hits)
