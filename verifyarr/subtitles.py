"""Subtitle parsing/comparison (srt/ass/ssa/vtt via pysubs2) + tokenizing for the
correctness check."""

from __future__ import annotations

import bisect
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
    # As common and content-free as "you're"/"she's" above, just missing from the original list --
    # found via real false positives where one of these, as a line's ONLY surviving token, was
    # enough on its own to flip a swap verdict (line_order.py: Slow Horses S01E02 #260, S01E03
    # #318). Two OTHER cases this same pattern caused (S01E02 #164, #196) turned out to be
    # dynamically catchable instead -- _judge_order discounts a word repeated on both sides of the
    # audio split, or shared between L1's and L2's own displayed text, before scoring -- so those
    # two needed no entry here at all. This static list is only the residual: a word that's
    # common-and-empty enough to be worthless evidence on its OWN, single, unrepeated appearance,
    # which no dynamic rule can catch. Deliberately NOT extended to every other contraction on
    # principle (tried that, "who's" cost a real detection -- S02E15 #50, "someone who's been
    # calling me a lesbian" -- its only surviving token, but a real, non-recurring one): only add
    # one of these when a real false positive actually traces back to it.
    "it's", "he's",
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


# A whisper segment whose ENTIRE text is a bracketed/parenthetical sound-effect or audio-condition
# tag -- "[screaming]", "(dramatic music)", "[BLANK_AUDIO]" -- rather than spoken dialogue. Real
# in production: seen up to 79 times in a single episode. These tokenize as ordinary words
# ("screaming", "music", "phone") and can spuriously match real subtitle lines that happen to share
# one. A leading "-" (dash-speaker marker) is stripped before checking, so "-(screams)" still
# counts. Text with a bracket ASIDE inside real dialogue ("Free tickets to (coughs) see...") does
# NOT match, since it isn't the whole segment.
_NONSPEECH_RE = re.compile(r"^-?\s*(?:[\[(][^\[\]()]*[\])]\s*)+$")


def is_nonspeech_annotation(text: str) -> bool:
    return bool(_NONSPEECH_RE.match((text or "").strip()))


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
    when there's no dialogue anywhere in the region at all (nothing better to do there).

    Window sums run over a prefix-sum array with binary search (O(n log n) total) instead of
    re-scanning every event per candidate (O(n²)) — this runs once per correctness sample,
    so the old shape dominated sampling cost on dialogue-heavy files. Candidates are still
    visited in subtitle order with a strict greater-than comparison, so tie-breaking is
    exactly as before (earliest candidate in file order wins)."""
    lo_ms, hi_ms = region_start * 1000, region_end * 1000
    in_region = [e for e in subs.events if lo_ms <= e.start <= hi_ms]
    if not in_region:
        return (region_start + region_end) / 2
    # Events ordered by start (stable, so equal starts keep file order) plus a prefix sum
    # of their character counts — one O(n log n) setup replacing the per-candidate scan.
    ordered = sorted(subs.events, key=lambda event: event.start)
    ordered_starts = [event.start for event in ordered]
    char_prefix = [0]
    for event in ordered:
        char_prefix.append(char_prefix[-1] + len(event.plaintext))
    best_start, best_score = None, -1
    for event in in_region:
        start = event.start / 1000.0
        # Same float expressions as the original scan (start * 1000, not event.start) so
        # window membership — including float-rounding edge cases — is bit-for-bit identical.
        window_lo_ms, window_hi_ms = start * 1000, (start + window_sec) * 1000
        lo_idx = bisect.bisect_left(ordered_starts, window_lo_ms)
        hi_idx = bisect.bisect_left(ordered_starts, window_hi_ms)
        score = char_prefix[hi_idx] - char_prefix[lo_idx]
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


# --- anchor-based resync -----------------------------------------------------------------------
# Anchors don't only say "this file is mis-timed" (correctness.significant_anchor_residuals) --
# each one is a measured offset at a known instant, so a run of agreeing anchors IS the correction
# for that stretch. These turn a list of anchored samples into an actual per-region shift plan.
#
# Two offsets belong to the same region if they agree within this much. Deliberately the same
# number as ANCHOR_SUSPECT_THRESHOLD_S: below it we would not have called the file mis-synced in
# the first place, so it cannot be worth splitting a region over.
ANCHOR_REGION_TOLERANCE_S = ANCHOR_SUSPECT_THRESHOLD_S

# A region has to be backed by at least this many agreeing anchors before its shift is applied --
# the same evidence bar significant_anchor_residuals uses to believe a mismatch at all. This is
# what refuses a file that DRIFTS continuously rather than shifting in blocks: measured on a real
# one (C_S02E19: +37, +22, +15, +6, +2, -1, -3, -12, -31, -39, -36 across the episode), no run of
# three consecutive anchors ever agrees, so no region qualifies and the file is left alone.
ANCHOR_REGION_MIN_ANCHORS = 3

# Below this there is nothing worth rewriting the file for -- same noise floor the comparison in
# pipeline._resolve_ambiguous_sync uses.
ANCHOR_RESYNC_MIN_SHIFT_S = ANCHOR_PREFER_MARGIN_S

# How far apart two sampled probes' measured offsets may sit before the file is treated as
# needing better evidence than a handful of clips can give. 5s, not ANCHOR_SUSPECT_THRESHOLD_S:
# this is a spread BETWEEN probes (a hop somewhere between them), not one probe's own residual.
# Measured over 52 episodes: a 4-probe screen at this threshold flags 9/9 files that genuinely
# need re-timing, with 1 false alarm in 43 -- independently reproduced by a separate
# implementation on the same library (16/16 flagged, 0/36 false).
ANCHOR_SCREEN_SPREAD_S = 5.0


def anchor_spread(samples: list[dict]) -> Optional[float]:
    """Widest disagreement between a file's confident anchors, or None with fewer than two."""
    vals = [s["anchor"]["shift"] for s in samples if s.get("anchor")]
    return (max(vals) - min(vals)) if len(vals) >= 2 else None


def anchor_points(samples: list[dict]) -> list[tuple[float, float]]:
    """[(clip_start_sec, shift_sec)] for every sample with a confident anchor, in time order."""
    pts = [(float(s["start"]), float(s["anchor"]["shift"]))
           for s in samples if s.get("anchor") and s.get("start") is not None]
    return sorted(pts)


def anchor_regions(points: list[tuple[float, float]],
                   tolerance: float = ANCHOR_REGION_TOLERANCE_S,
                   min_anchors: int = ANCHOR_REGION_MIN_ANCHORS) -> Optional[list[dict]]:
    """Groups anchors into consecutive runs that agree on one offset.

    Returns [{"lo", "hi", "shift", "n"}, ...] in time order, or None when ANY run is thinner than
    min_anchors -- "part of this file has an offset I can't verify" has to fail the whole plan,
    not be quietly skipped or averaged into its neighbour. A sparse run is exactly what a drifting
    file looks like, and also what a single mis-matched anchor looks like; neither is safe to act
    on, and both are better handled by the existing SUSPECT path. The one exception is a thin run
    whose median sits strictly BETWEEN its two disagreeing neighbours: that is the signature of a
    clip window straddling a region boundary (its median covers two offsets at once -- the same
    reason _resync_verified ignores cut-straddling anchors), so the anchor is dropped instead of
    vetoing the plan. Dropping is safe because the plan is still re-measured densely afterwards
    and discarded if it doesn't verify; the known blind spot is a miscorrection confined to less
    than ~2x clip_seconds around a cut (its anchors are cut-adjacent and excluded), bounded to
    the boundary neighbourhood instead of leaving minutes of the file broken.

    Grouping is greedy against the run's running median rather than against the previous point, so
    one noisy anchor inside an otherwise tight run doesn't split it in two."""
    if not points:
        return None
    runs: list[list[tuple[float, float]]] = [[points[0]]]
    for pos, shift in points[1:]:
        current = [s for _p, s in runs[-1]]
        if abs(shift - statistics.median(current)) <= tolerance:
            runs[-1].append((pos, shift))
        else:
            runs.append([(pos, shift)])
    # A run can end up within tolerance of its neighbour once both have their final medians
    # (the greedy pass only ever compared against the run as it stood at the time).
    merged: list[list[tuple[float, float]]] = [runs[0]]
    for run in runs[1:]:
        a = statistics.median([s for _p, s in merged[-1]])
        b = statistics.median([s for _p, s in run])
        if abs(a - b) <= tolerance:
            merged[-1] = merged[-1] + run
        else:
            merged.append(run)
    # A thin run BETWEEN two runs that agree with each other is one noisy anchor, not a region --
    # real on C_S03E11, where a single -6.6s reading at 680s sits in an otherwise flat stretch and
    # would otherwise veto correcting the whole file. Absorbed into its neighbours. A thin run
    # between two DIFFERENT offsets whose median sits strictly between them is a clip window
    # straddling the boundary -- real on C_S02E09 piecewise (6 clean runs of 8-9 anchors, one
    # straddling anchor per boundary), where it used to veto the whole plan. Dropped, not
    # absorbed: averaging it into either side would smear that side's shift. A thin run between
    # disagreeing neighbours whose value is NOT between them is genuinely undecidable (noise or
    # a third offset with no evidence) and still fails the plan below.
    def _median(run):
        return statistics.median([s for _p, s in run])

    prospect: list[list[tuple[float, float]]] = []
    for i, run in enumerate(merged):
        if 0 < i < len(merged) - 1 and len(run) < min_anchors:
            a, m, b = _median(merged[i - 1]), _median(run), _median(merged[i + 1])
            if abs(a - b) > tolerance and ((a < m < b) or (b < m < a)):
                continue
        prospect.append(run)
    cleaned: list[list[tuple[float, float]]] = []
    i = 0
    while i < len(prospect):
        run = prospect[i]
        neighbours_agree = (
            0 < i < len(prospect) - 1
            and abs(_median(prospect[i - 1]) - _median(prospect[i + 1])) <= tolerance)
        if len(run) < min_anchors and neighbours_agree:
            cleaned[-1] = cleaned[-1] + run + prospect[i + 1]
            i += 2
            continue
        cleaned.append(run)
        i += 1
    if any(len(run) < min_anchors for run in cleaned):
        return None
    return [{"lo": run[0][0], "hi": run[-1][0], "n": len(run),
             "shift": round(statistics.median([s for _p, s in run]), 2)} for run in cleaned]


def _cut_point(subs: "pysubs2.SSAFile", after_sec: float, before_sec: float) -> float:
    """Where to split two regions: the middle of the biggest gap between cues in
    (after_sec, before_sec). A real cut/insert sits in a pause, not mid-scene, and cutting in the
    widest pause is also what keeps the two differently-shifted halves from colliding. Falls back
    to the midpoint when there is no dialogue between the two anchors at all."""
    events = sorted((e for e in subs.events if after_sec * 1000 <= e.start <= before_sec * 1000),
                    key=lambda e: e.start)
    best, best_gap = None, -1.0
    for prev, nxt in zip(events, events[1:]):
        gap = (nxt.start - prev.end) / 1000.0
        if gap > best_gap:
            best, best_gap = (prev.end + nxt.start) / 2000.0, gap
    return best if best is not None else (after_sec + before_sec) / 2.0


def plan_anchor_resync(subs: "pysubs2.SSAFile", samples: list[dict]) -> Optional[list[dict]]:
    """The shift to apply to each stretch of `subs`, from its anchored samples -- or None when the
    evidence doesn't support correcting this file at all (see anchor_regions).

    Returns [{"lo_ms", "hi_ms", "shift"}, ...] covering the whole file end to end. Also None when
    every region is already inside the noise floor: nothing to fix."""
    regions = anchor_regions(anchor_points(samples))
    if not regions or all(abs(r["shift"]) < ANCHOR_RESYNC_MIN_SHIFT_S for r in regions):
        return None
    # The cut is applied to SUBTITLE times, but an anchor's position is an AUDIO time -- the two
    # differ by exactly that region's own shift. Searching the audio-time interval put the cut
    # before some cues that still needed the first region's shift (measured on C_S03E01: cut at
    # 191.6s instead of ~220s, leaving two anchors still 19s out afterwards).
    cuts = [_cut_point(subs, a["hi"] - a["shift"], b["lo"] - b["shift"])
            for a, b in zip(regions, regions[1:])]
    bounds = [float("-inf")] + [c * 1000.0 for c in cuts] + [float("inf")]
    return [{"lo_ms": bounds[i], "hi_ms": bounds[i + 1], "shift": r["shift"], "n": r["n"],
             # where this region's cut sits in AUDIO time, for callers re-measuring the result:
             # an anchor window straddling one of these covers two different offsets at once.
             "cut_audio_s": (cuts[i] + r["shift"]) if i < len(cuts) else None}
            for i, r in enumerate(regions)]


def apply_anchor_resync(subs: "pysubs2.SSAFile", plan: list[dict]) -> "pysubs2.SSAFile":
    """`subs` with each region's own shift applied, as a new file. Cues are re-sorted and
    de-overlapped afterwards for the same reason build_srt_from_segments does it: two regions
    moving by different amounts can push one cue past its neighbour, and two cues live at once
    renders as a doubled caption in most players."""
    import copy as _copy
    out = _copy.deepcopy(subs)
    for e in out.events:
        for region in plan:
            if region["lo_ms"] <= e.start < region["hi_ms"]:
                delta = round(region["shift"] * 1000)
                e.start = max(0, e.start + delta)
                e.end = max(e.start, e.end + delta)
                break
    out.sort()
    for current, following in zip(out.events, out.events[1:]):
        if current.end > following.start > current.start:
            current.end = following.start
    return out
