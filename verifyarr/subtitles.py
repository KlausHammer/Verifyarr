"""Subtitle parsing/comparison (srt/ass/ssa/vtt via pysubs2) + tokenizing for the
correctness check."""

from __future__ import annotations

import hashlib
import re
import statistics
import sys
from pathlib import Path
from typing import Optional

try:
    import pysubs2
except ImportError:  # pragma: no cover
    print("Missing the 'pysubs2' package. Run: pip install pysubs2", file=sys.stderr)
    raise

WORD_RE = re.compile(r"[a-zA-ZæøåÆØÅéèêëüöäßñçÉÈÊËÜÖÄ']{4,}")

# Common English filler words (>=4 chars, anything shorter is already excluded by WORD_RE)
# excluded from the correctness check's word overlap — see tokenize(). Without this, two
# completely unrelated dialogue excerpts often score 0.4-0.6 overlap by pure chance, since
# ordinary spoken-English grammar (were/that/about/come/still ...) alone gives a big overlap
# regardless of content — verified against a real mismatched subtitle where the score was
# 0.625 for an excerpt that was actually from a different episode entirely.
STOPWORDS = {
    "your", "yours", "yourself", "yourselves", "this", "that", "these", "those", "been", "being",
    "having", "doing", "and", "but", "because", "until", "while", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "from", "down", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "both", "each", "more", "most",
    "other", "some", "such", "only", "same", "than", "very", "will", "just", "don't", "should",
    "should've", "now", "aren't", "couldn't", "didn't", "doesn't", "hadn't", "hasn't", "haven't",
    "isn't", "mustn't", "needn't", "shouldn't", "wasn't", "weren't", "won't", "wouldn't", "yeah",
    "okay", "well", "like", "know", "gonna", "wanna", "gotta", "right", "really", "actually",
    "guess", "mean", "said", "says", "tell", "told", "look", "looks", "looking", "going", "come",
    "came", "still", "about", "something", "someone", "anything", "everything", "nothing", "thing",
    "things", "people", "guys", "they", "them", "their", "theirs", "what", "which", "were", "have",
    "with", "would", "could", "also", "even", "much", "many", "you're", "you've", "you'll", "you'd",
    "she's", "hers", "herself", "himself", "itself", "themselves", "whom",
}


def load_subs(path: Path) -> "pysubs2.SSAFile":
    last_err = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pysubs2.load(str(path), encoding=enc)
        except (UnicodeDecodeError, pysubs2.exceptions.Pysubs2Error) as e:
            last_err = e
            continue
    raise ValueError(f"Could not parse {path} ({last_err})")


def subs_fingerprint(subs: "pysubs2.SSAFile") -> str:
    """Stable hash of a subtitle's actual content (event timings + text) — lets a caller tell
    whether a subtitle has changed since a previous Whisper-based check, so expensive results from
    then (see line_order.py's collect_samples cache) can be safely reused instead of re-running
    Whisper. Deliberately content-based, not the file's mtime/size — those change even on a
    no-op re-sync (same timings, rewritten file), which would otherwise throw away a still-valid
    cache for nothing."""
    h = hashlib.sha256()
    for e in subs.events:
        h.update(f"{e.start}|{e.end}|{e.text}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def max_shift_stats(old: "pysubs2.SSAFile", new: "pysubs2.SSAFile"):
    old_ev, new_ev = list(old.events), list(new.events)
    n = min(len(old_ev), len(new_ev))
    if n == 0:
        return None, None, len(old_ev), len(new_ev)
    diffs = [abs(new_ev[i].start - old_ev[i].start) / 1000.0 for i in range(n)]
    return max(diffs), sum(diffs) / n, len(old_ev), len(new_ev)


def tokenize(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text or "")} - STOPWORDS


def subs_text_in_window(subs: "pysubs2.SSAFile", center_sec: float, before_sec: float, after_sec: float) -> str:
    lo_ms, hi_ms = (center_sec - before_sec) * 1000, (center_sec + after_sec) * 1000
    return "\n".join(e.plaintext for e in subs.events if lo_ms <= e.start <= hi_ms)


# Segments shorter than this (ms) are stretched to it -- a Whisper segment can legitimately be
# very short (a one-word interjection), but a cue that flashes for e.g. 80ms is unreadable and
# often just STT jitter rather than a real, separately-timed line. Matches common subtitle
# authoring guidance (~5 frames at 24-30fps is roughly this range) without pulling in a whole
# readability-timing library for what's a minor cosmetic floor.
MIN_CUE_DURATION_MS = 500


def build_srt_from_segments(segments: list[dict]) -> "pysubs2.SSAFile":
    """Builds a fresh SSAFile from Whisper-shaped segments ({"start","end","text"}, seconds) --
    used by generate.py to turn a full-track transcription (or its translation) into a subtitle
    ready for pysubs2's own .save(path) (which already handles SRT formatting everywhere else in
    this codebase). Empty/whitespace-only segments are dropped rather than written as blank
    cues; every kept cue is clamped to at least MIN_CUE_DURATION_MS.

    Cues are then sorted and de-overlapped, in that order, because the MIN_CUE_DURATION_MS floor
    can itself create an overlap: a one-word interjection 200ms before the next line gets
    stretched to 500ms and now runs past it. An overlap is not cosmetic the way a short cue is --
    two cues live at once renders as a doubled/flickering caption in most players, and sorting
    matters on its own because an SRT is defined as being in chronological order (the pysubs2
    writer emits events in list order, whatever that order happens to be)."""
    subs = pysubs2.SSAFile()
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start_ms = round(float(seg["start"]) * 1000)
        end_ms = round(float(seg["end"]) * 1000)
        end_ms = max(end_ms, start_ms + MIN_CUE_DURATION_MS)
        subs.append(pysubs2.SSAEvent(start=start_ms, end=end_ms, text=text))

    subs.sort()
    for current, following in zip(subs.events, subs.events[1:]):
        # Only trim when the next cue genuinely starts later -- two cues sharing a start time
        # are a different situation (simultaneous speakers), and clipping one to a 1ms sliver
        # would be worse than leaving both as they are.
        if current.end > following.start > current.start:
            current.end = following.start
    return subs


def pick_dialogue_dense_time(subs: "pysubs2.SSAFile", region_start: float, region_end: float,
                              window_sec: float) -> float:
    """Within [region_start, region_end] (seconds), pick the clip start time whose window_sec-long
    window contains the most subtitle dialogue (by character count) — avoids landing a correctness
    sample on a silent or dialogue-free stretch (action, score, an explosion) just because it
    happened to be the region's structural midpoint. Candidate start times are every subtitle
    event's own start within the region (checking every possible offset isn't worth it — dialogue
    density only meaningfully changes at event boundaries). Falls back to the region's midpoint
    when there's no dialogue anywhere in the region at all (nothing better to do there)."""
    lo_ms, hi_ms = region_start * 1000, region_end * 1000
    in_region = [e for e in subs.events if lo_ms <= e.start <= hi_ms]
    if not in_region:
        return (region_start + region_end) / 2
    best_start, best_score = None, -1
    for e in in_region:
        start = e.start / 1000.0
        window_hi_ms = (start + window_sec) * 1000
        score = sum(len(ev.plaintext) for ev in subs.events if start * 1000 <= ev.start < window_hi_ms)
        if score > best_score:
            best_score, best_start = score, start
    return best_start


# Anchor matching (see _match_segments_to_lines/_robust_clip_shift below, and the "Whisper ankre
# til sync-validering" plan): a Whisper segment only becomes a trusted "this timestamp is
# confirmed correct" anchor if it clears BOTH of these against one specific subtitle line --
# fraction of the LINE's own tokens found in the segment (same overlap() shape line_order.
# _judge_order already uses), and a minimum absolute count so one shared 4+ letter word alone
# (which can easily be a character name/generic word) never counts as evidence on its own.
# Provisional -- see the plan's Open Questions for calibrating against real segment data.
ANCHOR_MIN_OVERLAP = 0.5
ANCHOR_MIN_SHARED_TOKENS = 2

# A clip's own qualifying anchors must agree within this many seconds (median absolute
# deviation) before its shift estimate is trusted at all -- see _robust_clip_shift. Provisional,
# same caveat as above.
ANCHOR_MAX_MAD_SECONDS = 1.0

# Fewer distinct anchored LINES than this in one clip and _robust_clip_shift says nothing: with
# only two, one mis-timed segment drags the median halfway toward its own error (measured on a
# real, perfectly synced file: a single cue Whisper split into two segments gave a "confident"
# 1.0s shift with MAD 1.0), so a pair can never be trusted on its own.
ANCHOR_MIN_COUNT = 3

# How far a confident clip anchor may sit from the subtitle's own timing before it counts as a
# real mismatch (see correctness.significant_anchor_residuals and the candidate choice in
# pipeline._resolve_ambiguous_sync). Deliberately NOT sync.min_change_seconds (0.25s by default,
# alass's own apply threshold): anchors are built from Whisper SEGMENT starts, which routinely
# sit 0.5-1.5s off the cue they belong to (a segment can start mid-cue, or cover two or three
# short cues at once, and cues themselves lead the speech by a few hundred ms). Below this the
# anchor noise floor is louder than the signal, so a smaller value only produces false SUSPECTs.
ANCHOR_SUSPECT_THRESHOLD_S = 2.5

# When two sync candidates (see pipeline._resolve_ambiguous_sync) are compared on their anchor
# residuals, the challenger has to beat the default by at least this much before it wins --
# same noise floor argument as above: a residual difference smaller than this is not evidence
# of anything, and alass's own single-offset fit stays the default on a tie.
ANCHOR_PREFER_MARGIN_S = 1.0


def anchors_applicable(sub_lang: Optional[str], audio_lang: Optional[str]) -> bool:
    """Anchor matching is plain token overlap between what Whisper heard and what the subtitle
    line says -- it can only ever work when the subtitle is IN the spoken language (an English
    Whisper segment shares no 4+ letter words with the Danish line it corresponds to). The
    window-level correctness score gets around that by translating the window text (see
    correctness._compare_transcript_to_window); anchors are per-line and deliberately don't
    spend an LLM call each, so for a foreign-language subtitle there simply are no anchors --
    callers must treat that as "no timing evidence", never as "timing confirmed". Unknown on
    either side counts as applicable (nothing to contradict)."""
    if not sub_lang or not audio_lang:
        return True
    return sub_lang.lower().split("-")[0] == audio_lang.lower().split("-")[0]


def _match_segments_to_lines(segments: list[dict], clip_start_sec: float, subs,
                              window_start_sec: float, window_end_sec: float) -> list[dict]:
    """For each Whisper segment in this clip, finds the best-matching subtitle EVENT in
    [window_start_sec, window_end_sec] and, if the match clears ANCHOR_MIN_OVERLAP/
    ANCHOR_MIN_SHARED_TOKENS, turns it into an "anchor": a content-verified point estimate of
    the real timing offset at that instant (segment_start_abs - the matched line's OWN claimed
    start), independent of alass' blind audio-rhythm fit. See the anchor-sync plan for the full
    rationale -- this is deliberately NOT a new sync method, only a validator: alass sees the
    whole track's rhythm, this sees a handful of isolated, but individually PROVEN, instants.

    One anchor per LINE, from the EARLIEST segment that matches it: Whisper regularly splits a
    single cue into two or three segments, and every fragment after the first starts later than
    the cue by construction -- keeping them all as separate anchors would bias the clip's median
    by half the fragment gap on a perfectly synced file (see ANCHOR_MIN_COUNT).

    Lives here (not line_order.py, where it originated) because correctness.py needs it too
    and correctness.py is imported BY line_order.py -- putting it there would be circular.
    subtitles.py is the shared base both already import tokenize()/pick_dialogue_dense_time from.

    segments: {"start","end","text"} relative to clip_start_sec (Whisper's own verbose_json
    shape). window_* bound which subtitle EVENTS are even considered a candidate match
    (typically the same window correctness scoring already uses, see subs_text_in_window) --
    kept as an explicit param rather than re-deriving it here so a caller with a different
    clip-vs-window relationship (a heuristic slot, whose window is the cluster's own span, not
    correctness's usual +/-window_minutes) still gets correct anchors."""
    window_events = [e for e in subs.events
                      if window_start_sec * 1000 <= e.start <= window_end_sec * 1000]
    if not window_events:
        return []
    line_tokens_by_idx = [tokenize(e.plaintext) for e in window_events]

    anchored: dict[int, dict] = {}  # window_events index -> anchor (earliest matching segment)
    ordered = sorted((s for s in segments if isinstance(s, dict)),
                     key=lambda s: float(s.get("start") or 0.0))
    for seg in ordered:
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        seg_tokens = tokenize(seg_text)
        if not seg_tokens:
            continue
        seg_start_abs = clip_start_sec + float(seg.get("start") or 0.0)

        best_idx, best_overlap, best_shared = None, 0.0, 0
        for idx, line_tokens in enumerate(line_tokens_by_idx):
            if not line_tokens:
                continue
            shared = seg_tokens & line_tokens
            coeff = len(shared) / len(line_tokens)
            if coeff > best_overlap:  # strict: on a tie the EARLIEST line in the window wins
                best_idx, best_overlap, best_shared = idx, coeff, len(shared)

        if (best_idx is None or best_overlap < ANCHOR_MIN_OVERLAP
                or best_shared < ANCHOR_MIN_SHARED_TOKENS or best_idx in anchored):
            continue
        line_start_sec = window_events[best_idx].start / 1000.0
        anchored[best_idx] = {
            "segment_start_abs": round(seg_start_abs, 2),
            "matched_line_start_sec": round(line_start_sec, 2),
            "shift_sec": round(seg_start_abs - line_start_sec, 2),
            "overlap": round(best_overlap, 2),
        }
    return list(anchored.values())


def _robust_clip_shift(anchors: list[dict]) -> Optional[dict]:
    """Median + MAD (median absolute deviation) over a clip's own qualifying anchors' shifts --
    the substitute for the fitted-regression/RANSAC approach WhisperSync uses on much denser
    word-level data (see the anchor-sync plan): at our scale (a handful of anchors per clip,
    clustered in ~3 clips per file) there isn't enough spread to trust a fitted line, but a
    clip's anchors agreeing tightly with EACH OTHER is still real evidence its median shift is
    trustworthy. None (not a shaky number) when there's too little agreement to say anything --
    fewer than ANCHOR_MIN_COUNT qualifying anchors, or their spread exceeds
    ANCHOR_MAX_MAD_SECONDS."""
    shifts = [a["shift_sec"] for a in anchors]
    if len(shifts) < ANCHOR_MIN_COUNT:
        return None
    median = statistics.median(shifts)
    mad = statistics.median(abs(s - median) for s in shifts)
    if mad > ANCHOR_MAX_MAD_SECONDS:
        return None
    return {"shift": round(median, 2), "mad": round(mad, 2), "anchor_count": len(shifts)}


def clip_anchor_shift(segments: list[dict], clip_start_sec: float, subs,
                      window_start_sec: float, window_end_sec: float) -> Optional[dict]:
    """_match_segments_to_lines + _robust_clip_shift in one call -- the shape every caller
    actually wants (a clip's confident shift estimate, or None). See both for the details."""
    if not segments:
        return None
    return _robust_clip_shift(_match_segments_to_lines(segments, clip_start_sec, subs,
                                                       window_start_sec, window_end_sec))


def summarize_anchor_samples(samples: list[dict]) -> Optional[dict]:
    """Collapses a list of correctness samples ({"start", "anchor": clip_anchor_shift() | None,
    ...}) into one timing summary: {"regions": {start_sec: shift_sec}, "anchor_count",
    "mean_abs_shift"} over the samples that have a confident anchor -- None if none do. Used to
    compare sync CANDIDATES on the same audio samples (pipeline._resolve_ambiguous_sync):
    "regions" is keyed on the sample's clip start so two candidates' summaries can be compared
    on exactly the regions they BOTH have evidence in, which matters -- a candidate that's wrong
    by more than the window in some region has no anchors there at all (no line of its is close
    enough to be matched), and comparing plain averages would reward it for the gap."""
    regions: dict[float, float] = {}
    count = 0
    for s in samples:
        anchor = s.get("anchor")
        if not anchor or s.get("start") is None:
            continue
        regions[float(s["start"])] = float(anchor["shift"])
        count += int(anchor.get("anchor_count") or 0)
    if not regions:
        return None
    return {"regions": regions, "anchor_count": count,
            "mean_abs_shift": round(sum(abs(v) for v in regions.values()) / len(regions), 2)}
