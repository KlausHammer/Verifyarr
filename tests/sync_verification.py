"""Support module for verifying sync_engine/pipeline/correctness/line_order against real
Whisper data, at zero ongoing API cost -- built on the full-episode transcripts
tests/build_whisper_fixtures.py produces (see that file's docstring for why a FULL transcript,
not a few spot-checked clips, is what makes this trustworthy as ground truth).

Two independent things live here, both derived from the same fixture:

1. GroundTruth -- matches the real subtitle's own lines against the full transcript's segments,
   ONCE, densely (every line that has a confident match, not just the 3-6 the app itself would
   sample) -- see build_ground_truth(). Used to independently judge "is this candidate subtitle
   (the original, an alass resync, a deliberately desynced test copy, ...) actually correctly
   timed", without going anywhere near the app's own sync/correctness code -- so it can be used
   to check that code's own output without circularity.

2. WhisperShim + patch_whisper() -- stands in for the app's real STT calls (line_order.
   _extract_and_transcribe, correctness.extract_clip/transcribe_verbose) by slicing the SAME
   full transcript back into whatever clip the app asks for, exactly the way a real sweep asks
   for it (same call sites, same positions the app's own sampling logic picks -- collect_samples
   /correctness_check are not modified or bypassed, only the network call at the bottom is).
   This lets the FULL pipeline (sync_pair, collect_samples, correctness_and_finish,
   _resolve_ambiguous_sync) run end-to-end against dozens of synthetic scenarios per episode,
   for free, after the fixture was paid for once.

Approximation this rests on: slicing a WHOLE-EPISODE transcription (done in ~10-minute chunks)
back into a ~20-30s window is not byte-identical to what Whisper would produce if it only ever
saw that 20-30s window -- less context can occasionally change how a boundary word is heard.
The WORDS themselves are real Whisper output of the real audio, which is what every consumer in
this codebase actually reasons about (token overlap, anchor matching) -- segment-boundary
jitter at the edges of a slice is the only thing this doesn't reproduce, and it is a small
fraction of the noise a real per-clip call already has (see subtitles.ANCHOR_MAX_MAD_SECONDS).
"""

from __future__ import annotations

import contextlib
import json
import statistics
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import line_order as line_order_mod
from verifyarr import correctness as correctness_mod
from verifyarr.subtitles import load_subs, tokenize, ANCHOR_MIN_SHARED_TOKENS

MEDIA_DIR = Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 2")
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "whisper_full"

FIXTURE_SLUGS = ["S02E01", "S02E06", "S02E10", "S02E15", "S02E21"]

# Stricter than subtitles.ANCHOR_MIN_OVERLAP for the ground-truth builder specifically: matching
# against an ENTIRE episode's worth of lines (hundreds of candidates, not the handful a small
# window offers) raises the odds of a coincidental short-phrase match, so ground truth trades
# recall for precision here -- a line without a confident match is simply not used as ground
# truth, rather than risking a wrong one. Deliberately independent of the app's own thresholds
# in subtitles.py: this module's whole job is to judge that code, not reuse its exact tolerances.
GT_MIN_OVERLAP = 0.72
GT_MIN_SHARED_TOKENS = 3
# A local-neighborhood sanity pass (see build_ground_truth): an anchor whose residual disagrees
# with the median of anchors within this many seconds of it, by more than this margin, is very
# likely a wrong (coincidental-phrase) match and is dropped.
GT_LOCAL_WINDOW_SECONDS = 90.0
GT_LOCAL_DISAGREEMENT_SECONDS = 5.0


def load_fixture(slug: str) -> dict:
    path = FIXTURES_DIR / f"{slug}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist -- run: python3 tests/build_whisper_fixtures.py --episode {slug}")
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_paths(fixture: dict) -> tuple[Path, Path]:
    return MEDIA_DIR / fixture["video_name"], MEDIA_DIR / fixture["subtitle_name"]


class GroundTruth:
    """anchors: [{"line_index", "true_time_sec", "overlap"}, ...], one per original-subtitle
    line with a confident, locally-consistent match -- sorted by true_time_sec. line_index
    indexes into the ORIGINAL subtitle's own `.events` list (see build_ground_truth), so any
    candidate that keeps the same events in the same order (a pure timing change -- a global
    shift, a per-block resync, a deliberately broken test copy) can be judged against it just by
    looking up its own event at that same index; a candidate that reorders or rewrites lines
    (line swaps, a different subtitle entirely) cannot be judged this way -- see the module
    docstring's other use for that."""

    def __init__(self, fixture: dict, original_subs, anchors: list[dict]):
        self.fixture = fixture
        self.original_subs = original_subs
        self.anchors = anchors

    def residuals_for(self, candidate_subs, max_index: Optional[int] = None) -> list[tuple[int, float]]:
        """(line_index, residual_seconds) for every ground-truth anchor whose line_index is
        still valid in `candidate_subs` (same event count/order as the original) -- residual =
        candidate's own claimed start for that line MINUS the true (Whisper-confirmed) time.
        Positive = candidate's cue is too LATE; negative = too EARLY."""
        events = candidate_subs.events
        n = len(events) if max_index is None else max_index
        out = []
        for a in self.anchors:
            i = a["line_index"]
            if i >= n or i >= len(events):
                continue
            out.append((i, events[i].start / 1000.0 - a["true_time_sec"]))
        return out

    def summary_for(self, candidate_subs) -> Optional[dict]:
        """{"median_abs_residual", "mean_abs_residual", "max_abs_residual", "n"} over every
        anchor line still present in `candidate_subs` -- the single-number verdict most
        scenario assertions actually want. None if candidate_subs shares no anchored lines with
        the ground truth at all (e.g. a completely different subtitle)."""
        residuals = [r for _, r in self.residuals_for(candidate_subs)]
        if not residuals:
            return None
        abs_r = [abs(r) for r in residuals]
        return {"median_abs_residual": statistics.median(abs_r), "mean_abs_residual": sum(abs_r) / len(abs_r),
                "max_abs_residual": max(abs_r), "n": len(abs_r)}

    def true_time_near(self, t_sec: float, radius_sec: float = 120.0) -> Optional[float]:
        """Median TRUE time of whichever anchors' own true_time_sec falls within radius_sec of
        t_sec -- not directly useful on its own (residuals_for/summary_for are what scenario
        checks actually want), but handy for ad hoc inspection."""
        near = [a["true_time_sec"] for a in self.anchors if abs(a["true_time_sec"] - t_sec) <= radius_sec]
        return statistics.median(near) if near else None


def build_ground_truth(fixture: dict) -> GroundTruth:
    """Matches EVERY line of the real, original subtitle against the full transcript's Whisper
    segments -- see the module docstring and GT_* constants above. O(events x segments) token-set
    intersections (a few hundred x a few hundred here), a few hundred ms in pure Python."""
    _video, srt_path = fixture_paths(fixture)
    subs = load_subs(srt_path)
    segments = fixture["segments"]

    events = list(subs.events)
    event_tokens = [tokenize(e.plaintext) for e in events]

    raw: list[dict] = []  # one per event, at most -- earliest qualifying segment wins
    claimed: set[int] = set()
    # Segments in chronological order so "earliest segment wins" matches subtitles.
    # _match_segments_to_lines' own tie-breaking rule for a line spoken across several segments.
    for seg in sorted(segments, key=lambda s: s["start"]):
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        seg_tokens = tokenize(seg_text)
        if not seg_tokens:
            continue
        best_idx, best_overlap, best_shared = None, 0.0, 0
        for i, line_tokens in enumerate(event_tokens):
            if i in claimed or not line_tokens:
                continue
            shared = seg_tokens & line_tokens
            coeff = len(shared) / len(line_tokens)
            if coeff > best_overlap:
                best_idx, best_overlap, best_shared = i, coeff, len(shared)
        if best_idx is None or best_overlap < GT_MIN_OVERLAP or best_shared < GT_MIN_SHARED_TOKENS:
            continue
        claimed.add(best_idx)
        raw.append({"line_index": best_idx, "true_time_sec": round(float(seg["start"]), 2),
                    "overlap": round(best_overlap, 2)})

    raw.sort(key=lambda a: a["true_time_sec"])
    # Local-neighborhood sanity pass: an anchor whose own residual (against the SUBTITLE's own
    # claimed time for that line -- not against another candidate) disagrees with its
    # neighbors' median by more than GT_LOCAL_DISAGREEMENT_SECONDS is almost certainly a wrong
    # match (a short, repeated phrase matched to the wrong occurrence) and is dropped rather
    # than silently poisoning every later check against it.
    def own_residual(a: dict) -> float:
        return events[a["line_index"]].start / 1000.0 - a["true_time_sec"]

    clean = []
    for a in raw:
        neighborhood = [own_residual(b) for b in raw
                        if abs(b["true_time_sec"] - a["true_time_sec"]) <= GT_LOCAL_WINDOW_SECONDS]
        local_median = statistics.median(neighborhood)
        if abs(own_residual(a) - local_median) <= GT_LOCAL_DISAGREEMENT_SECONDS:
            clean.append(a)

    return GroundTruth(fixture, subs, clean)


class WhisperShim:
    """Serves clip_anchor_shift-shaped verbose_json responses by slicing a fixture's full
    transcript -- see patch_whisper(). `calls` records every (start_sec, duration_sec, language)
    requested, in order, so a scenario can assert how many/which clips the app actually sampled
    without needing a real network call to count."""

    def __init__(self, fixture: dict):
        self.segments = fixture["segments"]
        self.language = fixture["language"]
        self.calls: list[tuple[float, float, Optional[str]]] = []
        self._pending_clips: dict[str, tuple[float, float]] = {}

    def slice(self, start_sec: float, duration_sec: float, language: Optional[str] = None) -> dict:
        self.calls.append((round(start_sec, 2), round(max(duration_sec, 0.0), 2), language))
        lo, hi = start_sec, start_sec + duration_sec
        segs = []
        for s in self.segments:
            if s["end"] <= lo or s["start"] >= hi:
                continue
            segs.append({"start": round(max(s["start"], lo) - start_sec, 2),
                        "end": round(min(s["end"], hi) - start_sec, 2), "text": s["text"]})
        return {"language": language or self.language,
                "text": " ".join(s["text"] for s in segs), "segments": segs}


def _shim_llm_confirm_swaps(candidates, cfg, cancel_event=None):
    """Stands in for line_order._llm_confirm_swaps (the ONE call in this whole call graph that
    is an LLM chat completion, not an STT transcription -- a real key would be needed for it,
    which this test suite deliberately never requires). Returns "inconclusive" (None) for every
    candidate -- an honest "no real LLM opinion available" rather than fabricating a confident
    vote in either direction, which would silently bias whatever it's used to decide.
    Deliberately NOT "always agrees with Whisper": that was tried first and produced a false
    SUSPECT on an UNMODIFIED file, because Whisper's own audio judge has a real, non-zero rate
    of individually-inconclusive-but-still-flagged candidates even on clean input (ordinary
    noise, not a bug -- see _judge_order's own SWAP_MARGIN), and rubber-stamping every one of
    those with the fake LLM tipped it over _meets_swap_threshold. Safe for this suite's own
    swap-detection scenario regardless: per-LINE auto-fix (finalize_line_order's line_issues)
    is built from Whisper's verdicts alone and never touches the LLM path at all -- only the
    file-wide "swap_severity" escalation needs it, and with every candidate here abstaining,
    _meets_swap_threshold's own checked==0 guard means that escalation simply never fires,
    which is the correct behavior for an unavailable second opinion."""
    return {i: None for i, _l1, _l2 in candidates}


@contextlib.contextmanager
def patch_whisper(fixture: dict):
    """Monkey-patches the app's real Whisper AND line-order-LLM entry points for the duration of
    the `with` block: line_order._extract_and_transcribe (used by collect_samples for every slot
    kind), correctness.extract_clip + correctness.transcribe_verbose (used by correctness_check
    and, via delegation, detect_language_and_transcribe), and line_order._llm_confirm_swaps (see
    _shim_llm_confirm_swaps). Nothing else in the sync/correctness/line-order call graph makes a
    network call -- see evaluate_against_cached_transcripts and score_against_cached_transcripts,
    which only ever read the DB cache.

    Yields the WhisperShim so a test can inspect shim.calls afterward. Restores the real
    functions on exit even if the block raises."""
    shim = WhisperShim(fixture)
    real_extract_and_transcribe = line_order_mod._extract_and_transcribe
    real_extract_clip = correctness_mod.extract_clip
    real_transcribe_verbose = correctness_mod.transcribe_verbose
    real_llm_confirm_swaps = line_order_mod._llm_confirm_swaps

    def fake_extract_and_transcribe(video_path, start_sec, duration_sec, cfg, api_key, model,
                                    fallback, audio_lang, tmp_dir, cancel_event=None):
        return shim.slice(start_sec, duration_sec, language=audio_lang)

    def fake_extract_clip(video_path, start_sec, duration_sec, out_path) -> bool:
        out_path.write_bytes(b"RIFF" + b"\x00" * 2000)  # only existence/size>1000 is ever checked
        shim._pending_clips[str(out_path)] = (start_sec, duration_sec)
        return True

    def fake_transcribe_verbose(cfg, audio_path, language, cancel_event=None) -> dict:
        key = str(audio_path)
        pending = shim._pending_clips.pop(key, None)
        if pending is None:
            raise RuntimeError(
                f"WhisperShim: transcribe_verbose called for {audio_path!r} with no matching "
                "extract_clip call -- a real clip path was passed in, or extract_clip wasn't "
                "shimmed for this call site.")
        start_sec, duration_sec = pending
        return shim.slice(start_sec, duration_sec, language=language)

    line_order_mod._extract_and_transcribe = fake_extract_and_transcribe
    correctness_mod.extract_clip = fake_extract_clip
    correctness_mod.transcribe_verbose = fake_transcribe_verbose
    line_order_mod._llm_confirm_swaps = _shim_llm_confirm_swaps
    try:
        yield shim
    finally:
        line_order_mod._extract_and_transcribe = real_extract_and_transcribe
        correctness_mod.extract_clip = real_extract_clip
        correctness_mod.transcribe_verbose = real_transcribe_verbose
        line_order_mod._llm_confirm_swaps = real_llm_confirm_swaps
