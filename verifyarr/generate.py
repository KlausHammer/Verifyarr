"""Generate a brand-new subtitle for a video that has none at all (see discovery.discover_missing)
-- the one thing correctness.py's Whisper/LLM plumbing was never used for: this module reuses that
plumbing for a FULL-TRACK transcription (with segment-level timestamps) instead of a handful of
short sampled clips, then optionally translates the result into any other wanted language via an
LLM.

Three provider-shaped pieces, same split as correctness.py:
  - STT (speech-to-text): Groq/OpenRouter (both OpenAI-compatible /audio/transcriptions, reused
    almost verbatim from correctness.py) or Cloudflare Workers AI (a genuinely different request
    shape -- see _stt_adapter_cloudflare's docstring for the caveats).
  - LLM translation: Groq/OpenRouter/Gemini, all via correctness.translate_text (Gemini's
    OpenAI-compatible endpoint fits the exact same chat-completions shape as the other two).
  - Caching: db.video_full_transcript_cache -- a video's spoken-language transcript is reused
    across every wanted language, so only the (much cheaper) translation step repeats.

How the audio is cut up, in one place (the details are spread over the functions below):
  1. The whole audio track is decoded ONCE into a compressed mono 16kHz MP3 (extract_full_audio).
  2. ffmpeg's silencedetect runs ONCE over that MP3 (_detect_long_silences) -- every later step
     reuses that one silence map rather than re-analysing per chunk.
  3. Chunk boundaries are placed on a long silence wherever one sits near the target length
     (plan_chunks), so a chunk never cuts a word in half.
  4. Each chunk's actual upload is built by cutting ONLY its speech regions straight out of the
     full MP3 and concatenating them (trim_long_silences) -- long silences are never uploaded,
     and every piece carries its own ABSOLUTE original start so timestamps map straight back
     (_remap_through_trim).

Entry points used elsewhere:
  - generate_one() -- one (video, lang), used by both the manual/CLI single-file path (jobs.py's
    _run_generate_single, followed immediately by pipeline.sync_pair/finish_generated) and,
    indirectly, by...
  - run_generation_batch() -- the scheduled-sweep path (jobs._run_sweep), batching
    discover_missing's output, capped to cfg.generate_max_videos_per_day DISTINCT videos per
    rolling 24 hours to avoid burning a free API tier's whole daily quota.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from verifyarr import log, HEADER, SUCCESS
from verifyarr import db
from verifyarr import correctness
from verifyarr import fileops
from verifyarr.settings import Config
from verifyarr.subtitles import build_srt_from_segments, load_subs

# ISO 639-1 -> plain English name, for the translation prompt (correctness.translate_text takes
# a language NAME, not a code). Only the languages this app's own LANG3_TO_LANG2 already
# recognizes (see correctness.py) plus a couple of common additions -- an unlisted code just
# falls back to being used as-is (still an intelligible instruction to any capable LLM).
LANG_NAMES = {
    "en": "English", "da": "Danish", "sv": "Swedish", "no": "Norwegian", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "nl": "Dutch", "pt": "Portuguese",
    "fi": "Finnish", "is": "Icelandic", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ru": "Russian", "pl": "Polish", "cs": "Czech",
}
_LANG_NAME_TO_CODE = {name.lower(): code for code, name in LANG_NAMES.items()}

_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\.\s?(.*)$")

# A (video, lang) whose generation failed is not retried for this long -- without it, one
# permanently broken video (unreadable audio, no determinable spoken language) re-occupies one
# of the day's few generation slots on every single sweep, starving every other video behind
# it. A MANUAL "Generate" click ignores this entirely: a human asking for it right now is its
# own answer, same split as jobs._AUTO_TRIGGERS everywhere else in this app.
RETRY_COOLDOWN_HOURS = 24


class GenerationError(Exception):
    """Raised for any failure that should abort ONE (video, lang) generation attempt without
    taking down a whole batch -- caught by run_generation_batch per language."""


class TranscriptionError(GenerationError):
    """A failure in the (expensive) transcription stage specifically, as opposed to the much
    cheaper per-language translation stage. Worth its own type only so run_generation_batch can
    tell "this video is unusable, stop trying its other languages too" apart from "this ONE
    language failed to translate" -- see its loop."""


def normalize_lang(value: Optional[str]) -> Optional[str]:
    """Whatever a provider/ffprobe/settings field calls a language -> a plain ISO 639-1 code,
    or None when it isn't recognizable as one at all.

    Needed because the three sources disagree: ffprobe tags are ISO 639-2 ("dan"), Whisper's
    verbose_json reports a full English NAME ("danish") rather than a code, and this app's own
    settings hold 639-1 ("da"). Comparing those raw is how a generated English subtitle ends up
    being "translated" from English to English, and how a cached spoken_lang comes back in a
    shape the next chunk's `language` request parameter won't accept."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v in _LANG_NAME_TO_CODE:
        return _LANG_NAME_TO_CODE[v]
    if v in correctness.LANG3_TO_LANG2:
        return correctness.LANG3_TO_LANG2[v]
    if correctness.LANG_CODE_RE.match(v):
        return v
    log.warning("Unrecognized language value %r -- treating it as unknown", value)
    return None


def _tmp_tag(path: Path) -> str:
    """Short, filesystem-safe stand-in for a path's own name in temp filenames. Deliberately
    NOT the video's stem: a stem carrying an apostrophe ("Ocean's Eleven") breaks ffmpeg's
    concat-demuxer list format, and a very long one can push a temp path past the filesystem's
    name limit. Same md5-prefix idea sync_engine.resolve_alass_reference already uses."""
    return hashlib.md5(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:12]


# --- ffmpeg plumbing ---------------------------------------------------------------------------

_PROC_POLL_SECONDS = 0.5


def _run_tool(cmd: list[str], timeout: float, cancel_event=None) -> Optional[subprocess.CompletedProcess]:
    """subprocess.run for ffmpeg/ffprobe, except a cancel_event set mid-call KILLS the process
    instead of waiting for it. Without this, Cancel in the webapp has no effect for as long as
    the current ffmpeg call takes -- and unlike the alass calls elsewhere in this app (a few
    seconds each, see jobs.py), decoding a feature film's whole audio track is minutes, with a
    30-minute timeout behind it. Returns None on timeout/OSError, so callers keep their existing
    "falsy means it failed" contract; raises JobCancelled on cancellation."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_PROC_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        else:
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            proc.communicate()
            raise correctness.JobCancelled("cancelled during an ffmpeg call")
        if time.monotonic() >= deadline:
            proc.kill()
            proc.communicate()
            return None


# An ffmpeg output too small to hold any real audio -- an MP3 header alone is a few hundred
# bytes, so anything under this is an empty/failed extraction (e.g. a seek past the end of the
# track, which ffmpeg reports as success). Same guard correctness.extract_clip already applies.
MIN_AUDIO_BYTES = 1000


def _audio_written(out_path: Path) -> bool:
    try:
        return out_path.exists() and out_path.stat().st_size > MIN_AUDIO_BYTES
    except OSError:
        return False


def extract_full_audio(video_path: Path, out_path: Path, bitrate_kbps: int, timeout: int = 1800,
                        cancel_event=None) -> bool:
    """Decodes the video's audio ONCE into a compressed mono 16kHz MP3 -- compression is what
    lets a 90+ minute movie fit into a handful of provider-sized chunks instead of one huge WAV
    (a raw WAV would be ~15x the size for the same duration). Doing this once, up front, also
    means slicing the individual chunks below never has to re-decode the video itself."""
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
           "-b:a", f"{bitrate_kbps}k", "-f", "mp3", str(out_path)]
    proc = _run_tool(cmd, timeout, cancel_event=cancel_event)
    return proc is not None and proc.returncode == 0 and _audio_written(out_path)


def extract_chunk_from_audio(full_audio_path: Path, start_sec: float, duration_sec: float,
                              out_path: Path, timeout: int = 60, cancel_event=None) -> bool:
    """Slices one piece out of the already-extracted full-track MP3 via a plain stream copy (no
    re-encoding) -- fast, and accurate: unlike seeking directly in the source VIDEO (keyframe-
    aligned, and easily off by several seconds on a video with sparse keyframes), seeking within
    an audio-only MP3 has no such alignment concern."""
    if duration_sec <= 0:
        return False
    cmd = ["ffmpeg", "-y", "-ss", str(max(0.0, start_sec)), "-t", str(duration_sec),
           "-i", str(full_audio_path), "-c", "copy", "-f", "mp3", str(out_path)]
    proc = _run_tool(cmd, timeout, cancel_event=cancel_event)
    return proc is not None and proc.returncode == 0 and _audio_written(out_path)


def _concat_escape(path: Path) -> str:
    """One entry for ffmpeg's concat-demuxer list. Its format is shell-like but NOT shell: a
    single-quoted string ends at the next quote, so an embedded one has to be written as
    '\\'' (close, escaped quote, reopen). Temp names are md5-based (see _tmp_tag) so this
    should never actually have anything to escape -- but the enclosing temp DIRECTORY comes
    from TMPDIR, which this code doesn't control."""
    return str(path).replace("\\", "\\\\").replace("'", "'\\''")


# --- silence detection -------------------------------------------------------------------------
# A long silent stretch inside one Whisper request can make Whisper collapse the speech right
# before/after it into a single, wildly mistimed segment spanning the whole gap (observed: an
# 11s pause produced one segment covering 60.0-71.0s for a one-word line actually spoken at
# 69-70s). Trimming out anything longer than SILENCE_MIN_SECONDS before sending audio to Whisper
# avoids this AND skips paying for silence the model has nothing to transcribe anyway. This is a
# substitute for a real VAD/"is someone talking" signal -- alass itself has no exposed API for
# this (its own internal audio analysis is not surfaced by the alass-cli binary in any form), but
# ffmpeg's own silencedetect filter needs no new dependency and is good enough for this purpose.
SILENCE_MIN_SECONDS = 3.0  # only long pauses -- an ordinary conversational beat is left alone
SILENCE_NOISE_DB = -30.0
SILENCE_PAD_SECONDS = 0.2  # shrink each detected silence inward so a cut never clips real speech
# A "speech" region shorter than this is not speech -- it's the sliver left over between two
# silences that a pad couldn't fully absorb. Uploading one buys nothing and actively invites a
# hallucinated caption, which is Whisper's well-known response to near-silence.
MIN_SPEECH_REGION_SECONDS = 0.3

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _detect_long_silences(audio_path: Path, duration: float, cancel_event=None) -> list[tuple[float, float]]:
    """[(start, end), ...] silence intervals >= SILENCE_MIN_SECONDS long, via ffmpeg's
    silencedetect audio filter (a stderr-only diagnostic pass, "-f null -" writes no output).
    Run ONCE per video against the full extracted MP3 -- both the chunk boundaries and each
    chunk's own trimming read from this single map (see transcribe_full_track)."""
    cmd = ["ffmpeg", "-i", str(audio_path), "-af",
           f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}", "-f", "null", "-"]
    proc = _run_tool(cmd, 900, cancel_event=cancel_event)
    if proc is None:
        log.warning("Silence detection failed for %s -- continuing without it (chunks will be "
                    "cut at fixed offsets and will include their silences)", audio_path.name)
        return []
    silences = []
    pending_start = None
    for line in proc.stderr.splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            pending_start = float(m.group(1))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and pending_start is not None:
            silences.append((pending_start, float(m.group(1))))
            pending_start = None
    if pending_start is not None:  # silence ran to the end of the clip -- no closing marker for it
        silences.append((pending_start, duration))
    return _merge_silences(silences)


def _merge_silences(silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted, non-overlapping silences. Two silences that touch or overlap have to become one
    before they're inverted into speech regions -- otherwise the pad between them manufactures
    a fake sub-second "speech" region out of what is really one continuous silence."""
    ordered = sorted((min(a, b), max(a, b)) for a, b in silences)
    merged: list[tuple[float, float]] = []
    for s_start, s_end in ordered:
        if merged and s_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], s_end))
        else:
            merged.append((s_start, s_end))
    return merged


def _speech_regions_from_silences(duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Inverts silence intervals into the speech regions that cover [0, duration].

    Each silence's boundaries are padded INWARD by SILENCE_PAD_SECONDS so a cut never clips the
    start/tail of actual speech -- but only on a side that actually has speech to protect. A
    silence starting at 0 (or running to `duration`) borders nothing, and padding it anyway
    manufactures a 0.2s sliver of pure silence at the clip edge; for an entirely silent clip
    that turned two such slivers into a real Whisper request, against audio with nothing in it
    at all, which is the single most reliable way to get a hallucinated caption back."""
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in _merge_silences(silences):
        cut_start = s_start + SILENCE_PAD_SECONDS if s_start > 0.0 else s_start
        cut_end = s_end - SILENCE_PAD_SECONDS if s_end < duration else s_end
        cut_start = min(max(cut_start, cursor), duration)
        cut_end = min(max(cut_end, cursor), duration)
        if cut_start > cursor:
            speech.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
    if cursor < duration:
        speech.append((cursor, duration))
    return [(a, b) for a, b in speech if b - a >= MIN_SPEECH_REGION_SECONDS]


def _speech_regions_in_range(silences: list[tuple[float, float]], start: float,
                              end: float) -> list[tuple[float, float]]:
    """Same as _speech_regions_from_silences, but for the window [start, end) of a longer track
    and returning ABSOLUTE times. Used per chunk, against the video-wide silence map."""
    span = end - start
    if span <= 0:
        return []
    local = []
    for s_start, s_end in silences:
        clipped_start, clipped_end = max(s_start, start), min(s_end, end)
        if clipped_end > clipped_start:
            local.append((clipped_start - start, clipped_end - start))
    return [(a + start, b + start) for a, b in _speech_regions_from_silences(span, local)]


# --- chunk planning ----------------------------------------------------------------------------
# How far a chunk boundary may be pulled EARLIER than its target length to land in a silence.
# Only ever earlier, never later: a chunk is then guaranteed never to exceed the configured
# chunk length, which is the whole point of that setting for a provider with a hard per-request
# size limit (Cloudflare's in particular -- see _stt_adapter_cloudflare).
CHUNK_BOUNDARY_SNAP_SECONDS = 45.0
# A last chunk shorter than this is not worth a request of its own -- the remainder is split
# down the middle instead, so two sane chunks replace one full chunk plus a sliver.
MIN_TAIL_CHUNK_SECONDS = 15.0


def plan_chunks(duration: float, chunk_seconds: float,
                 silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[(start, end), ...] covering [0, duration], each at most chunk_seconds long, with every
    boundary placed in the MIDDLE of a long silence where one sits within
    CHUNK_BOUNDARY_SNAP_SECONDS before the target length.

    Cutting at a fixed offset instead means every boundary lands wherever it lands -- which, on
    a feature film, is mid-word roughly every time, since speech is most of the runtime. The
    word is then half in one request and half in the next, and Whisper renders both halves as
    whatever they sound like on their own. Snapping to silence costs nothing (the silence map
    is already computed for trimming) and makes the boundary a place where nothing is being
    said."""
    if duration <= 0:
        return []
    chunk_seconds = max(5.0, float(chunk_seconds))
    snap = min(CHUNK_BOUNDARY_SNAP_SECONDS, chunk_seconds / 4.0)
    midpoints = sorted((s + e) / 2.0 for s, e in silences)
    chunks: list[tuple[float, float]] = []
    cursor = 0.0
    while duration - cursor > chunk_seconds:
        remaining = duration - cursor
        # Two chunks' worth left: split the remainder down the middle rather than take one full
        # chunk and leave a sliver behind.
        target = cursor + (remaining / 2.0 if remaining <= chunk_seconds * 2 else chunk_seconds)
        candidates = [m for m in midpoints if target - snap <= m <= target]
        boundary = max(candidates) if candidates else target
        if boundary - cursor < MIN_TAIL_CHUNK_SECONDS:
            boundary = target  # a silence that close to the cursor would make a pointless chunk
        chunks.append((cursor, boundary))
        cursor = boundary
    if duration - cursor > 0.05:
        chunks.append((cursor, duration))
    return chunks


# --- silence trimming --------------------------------------------------------------------------

def trim_long_silences(full_audio_path: Path, start: float, end: float,
                        silences: list[tuple[float, float]], tmp_dir: Path, tag: str,
                        cancel_event=None) -> tuple[Optional[Path], list[tuple[float, float, float]]]:
    """Builds the audio actually uploaded for the chunk [start, end): every speech region in
    that window, cut straight out of the full-track MP3 and concatenated, with each long
    silence left out.

    Returns (audio_path, mapping). mapping is [(trimmed_start, trimmed_end, original_start),
    ...] where original_start is ABSOLUTE (seconds from the start of the video), so a timestamp
    measured against the uploaded audio converts straight back to the video's own timeline with
    no further chunk-offset arithmetic -- see _remap_through_trim. Returns (None, []) when the
    window holds no speech at all, which the caller should skip transcribing entirely rather
    than pay for an empty result.

    Each piece's mapped length is its MEASURED duration, not the duration that was requested:
    an MP3 frame at 16kHz is ~36ms and a stream copy can only cut on a frame boundary, so
    assuming the requested length silently accumulates that rounding across every piece in a
    chunk -- tens of pieces in, the drift is no longer negligible."""
    regions = _speech_regions_in_range(silences, start, end)
    if not regions:
        return None, []

    pieces: list[Path] = []
    mapping: list[tuple[float, float, float]] = []
    cursor = 0.0
    for i, (r_start, r_end) in enumerate(regions):
        piece_path = tmp_dir / f"{tag}.speech{i}.mp3"
        if not extract_chunk_from_audio(full_audio_path, r_start, r_end - r_start, piece_path,
                                         cancel_event=cancel_event):
            # Silently dropping this would remove that stretch of dialogue from the finished
            # subtitle with nothing anywhere to say why.
            log.warning("Could not cut the speech at %.0f-%.0fs out of the audio track -- that "
                        "stretch of dialogue will be missing from the generated subtitle",
                        r_start, r_end)
            continue
        actual = correctness.get_duration_seconds(piece_path) or (r_end - r_start)
        pieces.append(piece_path)
        mapping.append((cursor, cursor + actual, r_start))
        cursor += actual

    if not pieces:
        return None, []
    if len(pieces) == 1:
        # Nothing to concatenate -- the single extracted piece IS the chunk's audio. (Its
        # mapping still carries the real original start, which is not necessarily `start`: the
        # chunk may well open with a silence that was trimmed away.)
        return pieces[0], mapping

    concat_list = tmp_dir / f"{tag}.concat.txt"
    concat_list.write_text("".join(f"file '{_concat_escape(p)}'\n" for p in pieces), encoding="utf-8")
    trimmed_path = tmp_dir / f"{tag}.trimmed.mp3"
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy",
           str(trimmed_path)]
    try:
        proc = _run_tool(cmd, 300, cancel_event=cancel_event)
    finally:
        for p in pieces:
            p.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)
    if proc is None or proc.returncode != 0 or not _audio_written(trimmed_path):
        log.warning("Silence-trim concat failed for %.0f-%.0fs -- falling back to the untrimmed "
                    "chunk", start, end)
        trimmed_path.unlink(missing_ok=True)
        fallback = tmp_dir / f"{tag}.untrimmed.mp3"
        if not extract_chunk_from_audio(full_audio_path, start, end - start, fallback,
                                         cancel_event=cancel_event):
            return None, []
        return fallback, [(0.0, end - start, start)]
    return trimmed_path, mapping


def _remap_through_trim(seg_start: float, seg_end: float,
                         mapping: list[tuple[float, float, float]]) -> tuple[float, float]:
    """Converts a segment's [start, end) measured against the TRIMMED audio back to the video's
    own timeline, via the piece mapping from trim_long_silences. Located by the segment's START
    falling inside a mapped piece; if none matches (Whisper's own segmentation tends to respect
    the hard cuts a concat introduces, so this should be rare), falls back to the closest piece
    by trimmed start rather than dropping the segment."""
    if not mapping:
        return seg_start, seg_end
    for t_start, t_end, orig_start in mapping:
        if t_start <= seg_start < t_end:
            offset = orig_start - t_start
            return seg_start + offset, seg_end + offset
    t_start, _t_end, orig_start = min(mapping, key=lambda m: abs(m[0] - seg_start))
    offset = orig_start - t_start
    return seg_start + offset, seg_end + offset


# --- cue splitting ----------------------------------------------------------------------------
# Even with silence trimmed out (see trim_long_silences above), Whisper's own segmenter still
# sometimes returns one segment covering SEVERAL sentences with no internal split at all
# (observed in real testing: a 12-second segment reading "...you will also have to make a
# diorama Oh For your first assignment I would like you to form tribes..." as one unreadable
# block). This splits an oversized segment into several subtitle-shaped cues, distributing the
# ORIGINAL segment's own [start, end] proportionally across the split parts by character count --
# a plain heuristic (the same idea most caption tools use), not real per-word alignment, which
# the Whisper verbose_json response doesn't give us here.
CUE_MAX_CHARS = 84  # ~2 subtitle lines at a common ~42-chars/line guideline
CUE_MAX_SECONDS = 7.0  # a common upper bound for how long one cue should sit on screen
MIN_CUE_PIECE_SECONDS = 0.05

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _wrap_into_n_parts(text: str, n: int) -> list[str]:
    """Falls back to splitting on plain word boundaries, into `n` roughly-equal (by word count)
    pieces, when a segment has no sentence-ending punctuation to split on at all. `n` is driven
    by BOTH triggers (see split_segments_into_cues) -- a segment can need splitting for being too
    long in TEXT, too long in TIME, or both; a short-but-slow segment (e.g. one that straddled a
    trimmed-out silence -- see trim_long_silences) still needs cutting down even though its
    character count alone would fit under max_chars."""
    words = text.split()
    if n <= 1 or len(words) <= 1:
        return [text]
    n = min(n, len(words))
    base, extra = divmod(len(words), n)
    parts, idx = [], 0
    for i in range(n):
        take = base + (1 if i < extra else 0)
        parts.append(" ".join(words[idx:idx + take]))
        idx += take
    return [p for p in parts if p]


def _distribute_time(parts: list[str], start: float, end: float) -> list[dict]:
    """Spreads [start, end] across `parts` proportionally to each part's own character count.
    The very last part always inherits `end` exactly -- rounding across parts would otherwise
    drift the final cue's end away from what Whisper actually reported."""
    total_chars = sum(len(p) for p in parts) or 1
    duration = max(0.0, end - start)
    cues: list[dict] = []
    cursor = start
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        piece_end = end if is_last else min(end, cursor + duration * (len(part) / total_chars))
        piece_end = max(piece_end, cursor + MIN_CUE_PIECE_SECONDS)  # never zero/negative-length
        cues.append({"start": cursor, "end": piece_end, "text": part})
        cursor = piece_end
    return cues


_MAX_SPLIT_DEPTH = 4


def _split_until_short_enough(cue: dict, max_seconds: float, depth: int = 0) -> list[dict]:
    """Splits a cue until no piece sits on screen longer than max_seconds. Has to repeat rather
    than split once: _wrap_into_n_parts divides by WORD count while _distribute_time allocates
    time by CHARACTER count, so an even word split of "First sentence | here." hands 74% of the
    span to the first piece -- which can still be over the limit after a split that was sized as
    if the two halves would get equal time.

    Terminates on its own (a piece of one word is never split further, and each pass shortens
    the pieces); the depth cap is only a guard against a pathological input finding a way to
    make that untrue."""
    span = cue["end"] - cue["start"]
    if span <= max_seconds or depth >= _MAX_SPLIT_DEPTH or len(cue["text"].split()) < 2:
        return [cue]
    parts = _wrap_into_n_parts(cue["text"], max(2, math.ceil(span / max_seconds)))
    if len(parts) < 2:
        return [cue]
    out: list[dict] = []
    for piece in _distribute_time(parts, cue["start"], cue["end"]):
        out.extend(_split_until_short_enough(piece, max_seconds, depth + 1))
    return out


def split_segments_into_cues(segments: list[dict], max_chars: int = CUE_MAX_CHARS,
                              max_seconds: float = CUE_MAX_SECONDS) -> list[dict]:
    """A segment is only split when it actually needs it -- more than one sentence, OR longer
    than max_chars/max_seconds even as a single (possibly unpunctuated) run. Everything else
    passes through untouched.

    Splitting happens in two passes, because neither trigger alone is enough: a sentence split
    can still leave one 150-character sentence next to a two-word one (so each part is re-wrapped
    against max_chars), and parts that are short enough in TEXT can still each sit on screen far
    too long when the segment they came from spanned 20 seconds (so each finished piece is
    re-checked against max_seconds)."""
    out: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        # A provider reporting end < start must not make a negative cue -- and this has to be
        # applied to the PASS-THROUGH case too, not just to segments that get split, which is
        # the only path a short reversed segment actually takes.
        end = max(end, start)
        duration = end - start
        normalized = {**seg, "start": start, "end": end, "text": text}

        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
        needs_split = len(parts) > 1 or len(text) > max_chars or duration > max_seconds
        if not needs_split:
            out.append(normalized)
            continue

        # Pass 1 -- every sentence that is STILL oversized on its own gets wrapped further.
        expanded: list[str] = []
        for part in parts:
            if len(part) > max_chars:
                expanded.extend(_wrap_into_n_parts(part, math.ceil(len(part) / max_chars)))
            else:
                expanded.append(part)
        parts = [p for p in expanded if p]
        if len(parts) <= 1:
            # No punctuation to split on at all -- fall back to word boundaries, sized by
            # whichever of the two limits is the binding one.
            n = max(1, math.ceil(len(text) / max_chars), math.ceil(duration / max_seconds))
            parts = _wrap_into_n_parts(text, n)
        if len(parts) <= 1:
            out.append(normalized)
            continue

        # Pass 2 -- anything still on screen too long gets split again, within its own span.
        for cue in _distribute_time(parts, start, end):
            out.extend(_split_until_short_enough(cue, max_seconds))
    return out


# --- STT adapters -----------------------------------------------------------------------------
# Each adapter normalizes to {"language": Optional[str], "segments": [{"start","end","text", plus
# whatever confidence fields the provider reported}]} with chunk-LOCAL timestamps (seconds from
# the start of the uploaded audio) -- transcribe_full_track below maps those back onto the
# video's own timeline via trim_long_silences' mapping.

# Whisper's own per-segment confidence signals, as reported in verbose_json by Groq/OpenRouter.
# These are the ONLY quality signal available here that isn't itself Whisper -- a generated
# subtitle is never compared against an independent transcript the way a downloaded one is (see
# pipeline.finish_generated), so without them nothing at all stands between a hallucinated
# caption over the closing music and the finished .srt. Thresholds are OpenAI's own reference
# defaults (no_speech_threshold=0.6, logprob_threshold=-1.0, compression_ratio_threshold=2.4),
# including the rule that no-speech only disqualifies a segment when the model was ALSO unsure
# of what it heard -- a confidently transcribed line over quiet audio is real speech.
SEGMENT_MAX_NO_SPEECH_PROB = 0.6
SEGMENT_MIN_AVG_LOGPROB = -1.0
SEGMENT_MAX_COMPRESSION_RATIO = 2.4
_CONFIDENCE_FIELDS = ("no_speech_prob", "avg_logprob", "compression_ratio")


def _low_confidence_reason(seg: dict) -> Optional[str]:
    """Why this segment should be dropped as probable hallucination, or None to keep it. A
    provider that reports none of these fields (Cloudflare) always keeps everything."""
    no_speech = seg.get("no_speech_prob")
    avg_logprob = seg.get("avg_logprob")
    compression = seg.get("compression_ratio")
    if (no_speech is not None and no_speech > SEGMENT_MAX_NO_SPEECH_PROB
            and avg_logprob is not None and avg_logprob < SEGMENT_MIN_AVG_LOGPROB):
        return f"no_speech_prob={no_speech:.2f}, avg_logprob={avg_logprob:.2f}"
    if compression is not None and compression > SEGMENT_MAX_COMPRESSION_RATIO:
        # A high compression ratio means the text repeats itself -- Whisper's classic looping
        # failure ("Thank you. Thank you. Thank you. ...").
        return f"compression_ratio={compression:.2f}"
    return None


def _normalize_segment(raw: dict) -> dict:
    seg = {"start": float(raw["start"]), "end": float(raw["end"]),
           "text": (raw.get("text") or "").strip()}
    for field in _CONFIDENCE_FIELDS:
        value = raw.get(field)
        if isinstance(value, (int, float)):
            seg[field] = float(value)
    return seg


def _transcribe_chunk_openai_compat(provider: str, audio_path: Path, api_key: str, model: str,
                                     model_fallback: Optional[str], language: Optional[str],
                                     cancel_event=None, prompt: Optional[str] = None) -> dict:
    """Groq/OpenRouter -- both already return native per-segment timestamps in verbose_json, so
    this is a thin wrapper around correctness._transcribe_once with the same fallback-model-on-
    429 behavior as correctness.transcribe()."""
    try:
        result = correctness._transcribe_once(provider, audio_path, api_key, model, language=language,
                                               response_format="verbose_json", cancel_event=cancel_event,
                                               fail_fast_on_429=bool(model_fallback), prompt=prompt)
    except correctness.JobCancelled:
        raise
    except Exception as e:
        if not model_fallback or model_fallback == model:
            raise
        reason = "hit its rate limit" if isinstance(e, correctness.RateLimitExceeded) else f"failed ({e})"
        log.warning("%s generation STT with model %s %s, trying fallback %s", provider, model, reason, model_fallback)
        result = correctness._transcribe_once(provider, audio_path, api_key, model_fallback, language=language,
                                               response_format="verbose_json", cancel_event=cancel_event, prompt=prompt)
    segments = [_normalize_segment(s) for s in (result.get("segments") or [])]
    return {"language": result.get("language"), "segments": segments}


_CLOUDFLARE_RETRIES = 2


def _stt_adapter_cloudflare(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    """Cloudflare Workers AI's Whisper models are NOT OpenAI-compatible: the endpoint is
    account-scoped (`/accounts/{account_id}/ai/run/{model}`), auth is a Bearer API TOKEN (not
    the same kind of key as an OpenAI-style secret), and the documented request body is the raw
    audio as a JSON array of 8-bit unsigned integers rather than a multipart file upload. There
    is also no documented per-request size/duration cap -- community reports show a 45s/~700KB
    clip succeeding and a 4m28s/~4.2MB clip failing, so generate.chunk_seconds_cloudflare's
    default is deliberately small and SHOULD be tuned against your own account before relying on
    it for real generation runs.

    The response has no native per-sentence segments the way Groq/OpenRouter's verbose_json
    does -- Cloudflare's Whisper models return a WebVTT string in result.vtt instead, which is
    parsed here via the SAME pysubs2-based reader used everywhere else in this codebase
    (subtitles.load_subs), rather than writing a second VTT parser.

    Two limitations that are deliberately NOT worked around here, because neither can be
    verified against the real API from this codebase: the response carries no per-segment
    confidence fields, so the hallucination filter every other provider gets (see
    _low_confidence_reason) has nothing to act on; and `language` is not sent at all, since this
    endpoint's parameter set is undocumented and an unknown field is as likely to 400 the whole
    request as to be ignored. Language auto-detection is unreliable/absent here too -- always
    returns language=None, so the caller (transcribe_full_track) falls back to ffprobe's tag or
    generate.assume_spoken_lang.

    The account id is scrubbed out of every error path below. It isn't a secret, but it is a
    stable account identifier, and these strings end up in the Activity log and in a run's saved
    error_message."""
    account_id = cfg.generate_cloudflare_account_id
    api_token = cfg.generate_cloudflare_api_token
    model = cfg.generate_cloudflare_stt_model
    if not account_id or not api_token:
        raise GenerationError("Cloudflare account id/API token not configured")

    def _scrub(text: str) -> str:
        return text.replace(account_id, "<account-id>") if account_id else text

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}"}
    payload = {"audio": list(audio_path.read_bytes())}

    last_err = None
    for attempt in range(_CLOUDFLARE_RETRIES + 1):
        try:
            resp = correctness._post_ratelimited(url, headers, 90, json=payload, cancel_event=cancel_event)
        except correctness.JobCancelled:
            raise
        except Exception as e:
            last_err = _scrub(str(e))
        else:
            if resp.status_code == 200:
                break
            last_err = f"Cloudflare STT error {resp.status_code}: {_scrub(resp.text[:300])}"
            # 4xx is a request we built wrong (or a chunk over the undocumented size limit) --
            # retrying it changes nothing. Only a server-side failure is worth another attempt,
            # matching correctness._transcribe_once's own retry shape.
            if resp.status_code < 500:
                raise GenerationError(last_err)
        if attempt < _CLOUDFLARE_RETRIES:
            correctness.sleep_cancellable(2 * (attempt + 1), cancel_event)
    else:
        raise GenerationError(last_err or "Cloudflare STT failed")

    try:
        data = resp.json()
    except ValueError:
        raise GenerationError("Cloudflare STT returned a non-JSON response")
    result = data.get("result") or {}
    vtt_text = result.get("vtt")
    if not vtt_text:
        # Some Cloudflare Whisper models return only a flat "text" with no timing at all --
        # nothing usable to build cue timestamps from, so this chunk contributes nothing rather
        # than one giant mistimed cue.
        log.warning("Cloudflare STT response had no 'vtt' field for %s — chunk skipped", audio_path.name)
        return {"language": None, "segments": []}
    tmp_vtt = audio_path.with_suffix(".vtt")
    tmp_vtt.write_text(vtt_text, encoding="utf-8")
    try:
        subs = load_subs(tmp_vtt)
    finally:
        tmp_vtt.unlink(missing_ok=True)
    segments = [{"start": e.start / 1000.0, "end": e.end / 1000.0, "text": e.plaintext.strip()}
                for e in subs.events if e.plaintext.strip()]
    return {"language": None, "segments": segments}


def _adapter_groq(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    return _transcribe_chunk_openai_compat("groq", audio_path, cfg.generate_groq_api_key,
                                            cfg.generate_groq_stt_model, cfg.generate_groq_stt_model_fallback,
                                            language, cancel_event=cancel_event,
                                            prompt=cfg.generate_vocabulary_hint)


def _adapter_openrouter(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    return _transcribe_chunk_openai_compat("openrouter", audio_path, cfg.generate_openrouter_api_key,
                                            cfg.generate_openrouter_stt_model,
                                            cfg.generate_openrouter_stt_model_fallback,
                                            language, cancel_event=cancel_event,
                                            prompt=cfg.generate_vocabulary_hint)


def _adapter_cloudflare(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    return _stt_adapter_cloudflare(cfg, audio_path, language, cancel_event=cancel_event)


_STT_ADAPTERS = {"groq": _adapter_groq, "openrouter": _adapter_openrouter, "cloudflare": _adapter_cloudflare}


def _active_stt_model(cfg: Config) -> str:
    """Which STT model the CURRENT settings would use -- recorded alongside a cached transcript
    so switching model (not just provider) invalidates it, see db.get_full_transcript_cache."""
    return {
        "groq": cfg.generate_groq_stt_model,
        "openrouter": cfg.generate_openrouter_stt_model,
        "cloudflare": cfg.generate_cloudflare_stt_model,
    }.get(cfg.generate_stt_provider, cfg.generate_groq_stt_model)


# --- full-track transcription -------------------------------------------------------------------

def transcribe_full_track(cfg: Config, video_path: Path, tmp_dir: Path, cancel_event=None) -> dict:
    """Full-file transcription with absolute (whole-video) timestamps -- {"language", "segments"}.
    Chunked per cfg.generate_chunk_seconds_for(provider), on silence boundaries where possible
    (see plan_chunks); language is detected once (ffprobe's tag first, else whatever the first
    chunk's STT call reports, else generate.assume_spoken_lang) and held fixed for every later
    chunk -- the same "detect once, reuse" idiom as correctness.detect_language_and_transcribe."""
    duration = correctness.get_duration_seconds(video_path)
    if not duration:
        raise TranscriptionError(f"could not read duration (ffprobe): {video_path}")

    provider = cfg.generate_stt_provider
    adapter = _STT_ADAPTERS.get(provider)
    if adapter is None:
        raise TranscriptionError(f"unknown generate.stt_provider: {provider}")
    api_key = cfg.active_generate_stt_api_key
    if provider != "cloudflare" and not api_key:
        raise TranscriptionError(f"no API key configured for generate.stt_provider={provider}")

    chunk_seconds = max(30, cfg.generate_chunk_seconds_for(provider))
    tag = _tmp_tag(video_path)
    full_audio_path = tmp_dir / f"{tag}.full.mp3"
    if not extract_full_audio(video_path, full_audio_path, cfg.generate_audio_bitrate_kbps,
                               cancel_event=cancel_event):
        raise TranscriptionError(f"full audio extraction failed (unreadable or corrupt audio?): {video_path}")

    try:
        # The extracted MP3's own duration, not the container's -- they can differ slightly, and
        # every offset below is measured against the MP3.
        audio_duration = correctness.get_duration_seconds(full_audio_path) or duration
        silences = _detect_long_silences(full_audio_path, audio_duration, cancel_event=cancel_event)
        chunks = plan_chunks(audio_duration, chunk_seconds, silences)
        log.info("Transcribing %s: %.0f min of audio in %d chunk(s) via %s",
                  video_path.name, audio_duration / 60.0, len(chunks), provider)

        spoken_lang = normalize_lang(correctness.detect_audio_language_ffprobe(video_path))
        segments: list[dict] = []
        dropped = 0
        for i, (c_start, c_end) in enumerate(chunks):
            if cancel_event is not None and cancel_event.is_set():
                raise correctness.JobCancelled("cancelled during full-track transcription")

            trimmed_path, mapping = trim_long_silences(full_audio_path, c_start, c_end, silences,
                                                        tmp_dir, f"{tag}.c{i}", cancel_event=cancel_event)
            if trimmed_path is None:
                # Nothing but silence in this whole chunk -- no API call spent finding that out.
                log.debug("Chunk %d (%.0f-%.0fs) is silent — skipped", i, c_start, c_end)
                continue
            try:
                result = adapter(cfg, trimmed_path, spoken_lang, cancel_event=cancel_event)
            finally:
                trimmed_path.unlink(missing_ok=True)

            if spoken_lang is None:
                spoken_lang = normalize_lang(result.get("language"))
            for seg in result.get("segments") or []:
                if not seg.get("text"):
                    continue
                reason = _low_confidence_reason(seg)
                if reason:
                    dropped += 1
                    log.debug("Dropped a low-confidence segment at %.0fs (%s): %r",
                              c_start + seg["start"], reason, seg["text"][:80])
                    continue
                seg_start, seg_end = _remap_through_trim(seg["start"], seg["end"], mapping)
                # A segment may not run past its own chunk -- Whisper's end timestamps overshoot
                # routinely, and one bleeding into the next chunk's span puts cues out of order.
                seg_start = min(max(seg_start, c_start), c_end)
                seg_end = min(max(seg_end, seg_start), c_end)
                segments.append({"start": seg_start, "end": seg_end, "text": seg["text"]})

        if dropped:
            log.info("Dropped %d low-confidence segment(s) Whisper was unsure of (probable "
                      "hallucination over music/silence)", dropped)
        if spoken_lang is None:
            spoken_lang = normalize_lang(cfg.generate_assume_spoken_lang)
        segments.sort(key=lambda s: (s["start"], s["end"]))
        return {"language": spoken_lang, "segments": split_segments_into_cues(segments)}
    finally:
        full_audio_path.unlink(missing_ok=True)


# --- translation -----------------------------------------------------------------------------
# One LLM call carries a numbered list of subtitle lines. correctness.translate_text TRUNCATES
# its user content at max_chars, which for a numbered list would silently drop the tail -- so
# batches are sized in CHARACTERS as well as in lines (see _iter_batches), never truncated.
TRANSLATE_MAX_CHARS = 8000
_NUMBER_PREFIX_COST = 8  # "123. " plus the newline, rounded up


def _generate_llm_model_and_fallback(cfg: Config) -> tuple[str, Optional[str]]:
    provider = cfg.generate_llm_provider
    if provider == "gemini":
        return cfg.generate_gemini_llm_model, cfg.generate_gemini_llm_model_fallback
    if provider == "openrouter":
        return cfg.generate_openrouter_llm_model, cfg.generate_openrouter_llm_model_fallback
    return cfg.generate_groq_llm_model, cfg.generate_groq_llm_model_fallback


def _translate_one(cfg: Config, text: str, target_lang_name: str, cancel_event=None) -> Optional[str]:
    provider = cfg.generate_llm_provider
    llm_model, llm_fallback = _generate_llm_model_and_fallback(cfg)
    return correctness.translate_text(text, target_lang_name, provider=provider,
                                       api_key=cfg.active_generate_llm_api_key, llm_model=llm_model,
                                       llm_model_fallback=llm_fallback, cancel_event=cancel_event)


def _iter_batches(segments: list[dict], batch_size: int, max_chars: int) -> Iterator[list[dict]]:
    """Batches of at most `batch_size` lines AND at most `max_chars` of numbered input. The
    character bound is what keeps translate_text from truncating: a numbered list cut off
    mid-way silently loses its trailing lines, which _translate_batch's own count check would
    then reject, dropping the whole batch to one-line-at-a-time for no reason."""
    batch: list[dict] = []
    size = 0
    for seg in segments:
        cost = len(seg["text"]) + _NUMBER_PREFIX_COST
        if batch and (len(batch) >= batch_size or size + cost > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(seg)
        size += cost
    if batch:
        yield batch


def _parse_numbered_reply(raw: Optional[str], expected: int) -> Optional[list[str]]:
    """The `expected` translated lines, or None if the reply doesn't match the numbered
    structure exactly. A line the model wrapped across two physical lines is joined back
    together (dropping the continuation would silently truncate that subtitle line while still
    passing a naive count check); a repeated number is a renumbering error and fails."""
    if not raw:
        return None
    parsed: dict[int, list[str]] = {}
    current: Optional[int] = None
    for line in raw.splitlines():
        m = _NUMBERED_LINE_RE.match(line)
        if m:
            number = int(m.group(1))
            if number in parsed:
                return None  # same number twice -- the mapping to timestamps is no longer trustworthy
            current = number
            parsed[current] = [m.group(2).strip()]
        elif current is not None and line.strip():
            parsed[current].append(line.strip())
    if len(parsed) != expected or any(i + 1 not in parsed for i in range(expected)):
        return None
    return [" ".join(p for p in parsed[i + 1] if p).strip() for i in range(expected)]


def _translate_batch(cfg: Config, batch: list[dict], target_lang_name: str,
                     cancel_event=None) -> list[Optional[str]]:
    """One LLM call for a batch of lines at once (a numbered list) -- far cheaper on rate limits
    than one call per line. Validated strictly: the response must have exactly len(batch)
    numbered lines, 1..N, in order -- anything else (merged lines, dropped lines, renumbering)
    falls back to translating this batch one line at a time, so a translated line can NEVER land
    on the wrong timestamp.

    A line that could not be translated at all comes back as None rather than as its untranslated
    source text -- see translate_segments for why that distinction matters."""
    numbered_in = "\n".join(f"{i + 1}. {seg['text']}" for i, seg in enumerate(batch))
    system_prompt = (
        f"You translate subtitle lines to {target_lang_name}. The user gives {len(batch)} numbered "
        "lines. Reply with exactly that many numbered lines, same numbers, same order, one line per "
        "number, formatted 'N. translated text'. Do not merge, split, drop, or add lines. No other text."
    )
    provider = cfg.generate_llm_provider
    llm_model, llm_fallback = _generate_llm_model_and_fallback(cfg)
    raw = correctness.translate_text(numbered_in, target_lang_name, provider=provider,
                                      api_key=cfg.active_generate_llm_api_key, llm_model=llm_model,
                                      llm_model_fallback=llm_fallback, system_prompt=system_prompt,
                                      max_chars=TRANSLATE_MAX_CHARS, cancel_event=cancel_event)
    parsed = _parse_numbered_reply(raw, len(batch))
    if parsed is not None:
        # A structurally valid reply can still carry an EMPTY translation for a line that had
        # real text -- a content filter, or a model that just skipped one. That is a failed
        # line, not a line whose translation happens to be blank; writing it through would drop
        # that subtitle line silently while every count check still passed.
        return [text if text or not seg["text"].strip() else None
                for text, seg in zip(parsed, batch)]

    log.warning("Batch translation to %s came back malformed — falling back to one-line-at-a-time "
                "for these %d line(s)", target_lang_name, len(batch))
    out: list[Optional[str]] = []
    for seg in batch:
        translated = _translate_one(cfg, seg["text"], target_lang_name, cancel_event=cancel_event)
        out.append(translated if translated else None)
    return out


# How many lines may fail to translate before the whole file is abandoned. Zero, deliberately:
# the alternative (the old behavior) was to fall back to the UNTRANSLATED source text for a
# failed line, which writes a Danish subtitle with English lines scattered through it -- and
# nothing downstream would ever catch that, since the correctness check is skipped for generated
# files (pipeline.finish_generated) and would have scored the English lines against English
# audio as a perfect match anyway. Failing is cheap here: the transcript is already cached, so a
# retry pays only for the translation.
MAX_TRANSLATION_FAILURES = 0


def translate_segments(cfg: Config, segments: list[dict], target_lang: str, cancel_event=None) -> list[dict]:
    """Translates every segment's text to target_lang (an ISO 639-1-ish code, e.g. "da"),
    keeping each segment's own start/end UNCHANGED -- this is a text-only step on already-timed
    segments, never a re-sync, so it can't introduce drift the way translating a whole separate
    file with its own timing would.

    Raises GenerationError if more than MAX_TRANSLATION_FAILURES lines could not be translated
    at all, rather than writing a half-translated file."""
    target_lang_name = LANG_NAMES.get(target_lang, target_lang)
    batch_size = max(1, cfg.generate_translate_batch_size)
    out: list[dict] = []
    failures = 0
    for batch in _iter_batches(segments, batch_size, TRANSLATE_MAX_CHARS):
        translated_texts = _translate_batch(cfg, batch, target_lang_name, cancel_event=cancel_event)
        for seg, text in zip(batch, translated_texts):
            if text is None:
                failures += 1
                continue
            out.append({**seg, "text": text})
    if failures > MAX_TRANSLATION_FAILURES:
        raise GenerationError(
            f"{failures} of {len(segments)} line(s) could not be translated to {target_lang_name} — "
            "no file written rather than one with untranslated lines left in it (the transcript is "
            "cached, so retrying only pays for the translation)")
    return out


# --- entry points ------------------------------------------------------------------------------

def _transcribe_or_reuse(cfg: Config, video_path: Path, tmp_dir: Path, conn,
                          cancel_event=None) -> tuple[Optional[str], list[dict]]:
    """(spoken_lang, segments) for this video -- from db.video_full_transcript_cache when the
    cached entry still matches the video AND the current STT provider/model, otherwise freshly
    transcribed and cached."""
    provider, model = cfg.generate_stt_provider, _active_stt_model(cfg)
    cached = db.get_full_transcript_cache(conn, video_path, stt_provider=provider, stt_model=model)
    if cached is not None:
        log.info("Reusing the cached full transcript for %s (%d segments) — no Whisper calls needed",
                  video_path.name, len(cached["segments"]))
        # Normalized (and re-defaulted) on READ as well as on write: a transcript cached before
        # generate.assume_spoken_lang was filled in would otherwise stay unusable for its whole
        # 90-day retention, with the setting that fixes it quietly having no effect.
        spoken = normalize_lang(cached["spoken_lang"]) or normalize_lang(cfg.generate_assume_spoken_lang)
        return spoken, cached["segments"]

    result = transcribe_full_track(cfg, video_path, tmp_dir, cancel_event=cancel_event)
    spoken_lang, segments = result["language"], result["segments"]
    if not segments:
        raise TranscriptionError(
            f"transcription produced no usable segments for {video_path} — the audio may be "
            "silent, unreadable, or entirely music")
    db.save_full_transcript_cache(conn, video_path, spoken_lang, segments,
                                   stt_provider=provider, stt_model=model)
    return spoken_lang, segments


def _generate_one(cfg: Config, video_path: Path, lang: str, tmp_dir: Path, conn,
                   cancel_event=None) -> Optional[Path]:
    spoken_lang, segments = _transcribe_or_reuse(cfg, video_path, tmp_dir, conn, cancel_event=cancel_event)
    if spoken_lang is None:
        raise TranscriptionError(
            f"could not determine the spoken language for {video_path} — set generate.assume_spoken_lang "
            "or check the video's audio-stream language tag")

    want = normalize_lang(lang) or lang
    final_segments = segments if want == spoken_lang else translate_segments(
        cfg, segments, want, cancel_event=cancel_event)
    subs = build_srt_from_segments(final_segments)
    if not subs.events:
        raise GenerationError(f"nothing left to write after building cues for {video_path}")
    if cfg.dry_run:
        log.info("[dry-run] would write %s.%s.srt for %s (%d cues)",
                  video_path.stem, want, video_path.name, len(subs.events))
        return None
    return fileops.write_new_subtitle(subs, video_path, want)


def generate_one(cfg: Config, video_path: Path, lang: str, tmp_dir: Path, conn,
                  cancel_event=None) -> Optional[Path]:
    """Generates (or reuses a cached transcript for) one (video, lang) and writes the resulting
    subtitle file -- the one function used by BOTH the batched sweep and the manual/CLI single
    trigger. Raises GenerationError/fileops.SubtitleAlreadyExists/correctness.JobCancelled on
    failure -- callers decide whether that aborts one file or the whole batch.

    Returns None instead of writing anything when cfg.dry_run is on -- transcription/translation
    still runs (so a dry-run preview costs the same API calls a real run would, and any log/report
    output still reflects what WOULD be generated), matching pipeline.py's own dry_run convention
    elsewhere in this app (compute the real result, skip only the actual write).

    Every outcome except cancellation is recorded in db.generate_attempts -- that is what stops
    one permanently broken video from re-consuming a scarce daily generation slot on every
    sweep, and what makes the daily cap a real cap rather than a per-run one."""
    try:
        dest = _generate_one(cfg, video_path, lang, tmp_dir, conn, cancel_event=cancel_event)
    except correctness.JobCancelled:
        raise  # not an outcome for this (video, lang) at all -- nothing was decided
    except Exception as e:
        db.record_generate_attempt(conn, video_path, lang, ok=False, error=str(e))
        raise
    db.record_generate_attempt(conn, video_path, lang, ok=True, error=None)
    return dest


def run_generation_batch(conn, run_id: int, cfg: Config, missing: list[tuple[Path, str]],
                          cancel_event=None) -> int:
    """Groups `missing` (video, lang) pairs by video, caps to cfg.generate_max_videos_per_day
    DISTINCT videos across the last 24 HOURS (transcription cost is per-video -- generating
    several extra wanted languages for an already-transcribed video is comparatively cheap), and
    generates each. Returns how many files were written.

    The cap is deliberately a rolling-day one rather than a per-run one: the Bazarr wanted-
    subtitles poll starts a fresh sweep every time ANY wanted item resolves (see bazarr_poll.py),
    so a per-run cap on a busy day is no cap at all -- it multiplies by however many times that
    poll happened to fire.

    Deliberately does NOT sync/verify the new file within this same run -- it's left as an
    ordinary external subtitle for the NEXT sweep's discover_pairs()/discover_missing() to pick
    up naturally, so this batch's own rate-limit budget isn't shared with a second feature
    mid-run. See jobs._run_generate_single for the manual path, which DOES chain straight into
    sync + finish_generated for immediate feedback."""
    by_video: "OrderedDict[Path, list[str]]" = OrderedDict()
    for video, lang in missing:
        by_video.setdefault(video, []).append(lang)

    cooldown_since = (datetime.now(timezone.utc) - timedelta(hours=RETRY_COOLDOWN_HOURS)).isoformat()
    day_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    already_today = db.count_generated_videos_since(conn, day_since)
    cap = cfg.generate_max_videos_per_day
    budget = max(0, cap - already_today)
    if budget <= 0:
        if cap <= 0:
            log.info("Subtitle generation skipped — Max. videos per day is set to 0 "
                      "(Settings -> Generate)")
        else:
            log.info("Subtitle generation skipped — %d of the %d video(s) allowed per day have "
                      "already been generated in the last 24 hours (Settings -> Generate -> "
                      "Max. videos per day)", already_today, cap)
        return 0

    # A (video, lang) that failed recently doesn't get to occupy one of those few slots again --
    # and a video whose every wanted language is in that state is skipped entirely rather than
    # counted against the budget.
    planned: list[tuple[Path, list[str]]] = []
    skipped_videos = 0
    for video, langs in by_video.items():
        if len(planned) >= budget:
            break
        fresh = [l for l in langs if not db.generate_failed_since(conn, video, l, cooldown_since)]
        if not fresh:
            skipped_videos += 1
            continue
        planned.append((video, fresh))
    if skipped_videos:
        log.info("Skipped %d video(s) whose generation failed within the last %d hours — they'll "
                  "be retried after that", skipped_videos, RETRY_COOLDOWN_HOURS)
    if not planned:
        return 0

    written = 0
    with tempfile.TemporaryDirectory(prefix="verifyarr-generate-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for video, langs in planned:
            if cancel_event is not None and cancel_event.is_set():
                raise correctness.JobCancelled("cancelled during subtitle generation")
            log.log(HEADER, "▸ Generating subtitle(s) for %s: %s", video.name, ", ".join(langs))
            for lang in langs:
                try:
                    dest = generate_one(cfg, video, lang, tmp_dir, conn, cancel_event=cancel_event)
                except correctness.JobCancelled:
                    raise
                except fileops.SubtitleAlreadyExists as e:
                    log.info("%s", e)
                    continue
                except TranscriptionError as e:
                    # The transcript is what failed, not this one language -- every other wanted
                    # language for this video would re-run the exact same (whole-file, expensive)
                    # transcription and fail exactly the same way.
                    log.error("Generation failed for %s: %s — skipping its other language(s) too",
                              video.name, e)
                    break
                except Exception as e:
                    log.error("Generation failed for %s (%s): %s", video.name, lang, e)
                    continue
                if dest is None:
                    continue  # dry-run -- already logged inside generate_one, nothing written
                log.log(SUCCESS, "Generated %s", dest.name)
                db.bump_run_generated(conn, run_id)
                written += 1
    return written
