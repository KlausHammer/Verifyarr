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


_ASIDE_RE = re.compile(r"[\[(][^\[\]()]*[\])]")


def speech_text(text: str) -> str:
    """Text that is spoken dialogue: lines with a music note and every bracketed
    aside ("[VOMITING]", "(music)") removed. Not every subtitle or model marks songs
    and sounds, so what is marked never counts -- on either side."""
    lines = (text or "").replace("\\N", "\n").split("\n")
    return _ASIDE_RE.sub(" ", " ".join(ln for ln in lines if "♪" not in ln and "♫" not in ln))


def tokenize(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(speech_text(text))} - STOPWORDS


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


# Anchor matching (see _match_segments_to_lines/_robust_clip_shift below, and the "Whisper anchors
# for sync validation" plan): a Whisper segment only becomes a trusted "this timestamp is
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

# Framerate (1001/1000) tilt detection. A 24fps subtitle on 23.976fps audio (or the
# reverse) drifts 0.1% end to end -- ~1.3s over a 21-minute episode. The decision
# quantity is the ACCUMULATED TILT in seconds (Theil-Sen slope times span), not the
# slope: dividing by a varying span sprays variance into the estimate. Two
# independent-ish estimators must agree (see pipeline._try_fps_rescale):
#   * anchor tilt from pooled raw (audio, subtitle) anchor points over all clips --
#     deliberately NOT clip medians: 52-57% of 15s clips fall below ANCHOR_MIN_COUNT
#     and would be dropped.
#   * VAD tilt: same estimator over (cue start, nearest speech-onset delta) points.
# Calibrated with production sampling (dialogue-dense 16x15s clips, +-30s windows):
# real tilts -1.40/-1.40/-1.20/+0.97s, healthy anchor max +0.72s, healthy VAD range
# -0.55..+0.32s (sparse clip-cache onsets, the runtime source). The 0.50 anchor
# floor alone lets 2/31 healthy through; requiring
# VAD confirmation (same sign, >= 0.30s) vetoes both while keeping 3/4 real
# (C_S02E11, the one opposite-direction file, is VAD's blind spot).
FPS_RATIOS = ((1000 / 1001, "24 -> 23.976"), (1001 / 1000, "23.976 -> 24"))
# Raised from 0.50 after the matrix caught the one false positive 0.50 let through: SH_S01E06,
# a healthy file, tilted 0.52s under the `gap` scenario (which deletes 300s from the middle and
# skews the anchor geometry) and its rescale halved that file's recovery (1.000 -> 0.441). The
# four genuinely framerate-shifted episodes sit at 0.97-1.54, so 0.90 rejects the false one with
# 0.38s to spare and keeps every real one. Validating only against untouched subtitles is what
# hid this -- the detector has to clear the OTHER corruption scenarios too.
FPS_ANCHOR_TILT_MIN_S = 0.90
FPS_BINNED_TILT_MIN_S = 0.90
FPS_LOO_TILT_MIN_S = 0.70
FPS_VAD_TILT_MIN_S = 0.30
FPS_MIN_ANCHORS = 20
FPS_ANCHOR_TRIM_S = 8.0
FPS_MAX_BASE_SPREAD_S = 1.0
FPS_BINS = 16
FPS_MIN_VOTES = 8
FPS_LOO_PARTS = 8
FPS_VAD_MAXGAP_S = 2.0
FPS_VAD_TRIM_S = 1.5
FPS_VAD_MIN_POINTS = 40
# Monotonicity is measured (anchor_drift_signature returns "rho") but deliberately
# NOT gated on here. On the four real 0.1% files rho is only 0.48-0.68, while the
# healthy file with the largest tilt (C_S03E05) reaches 0.74 -- a floor would veto a
# real case before a false one. At 0.1% the offset moves 1.2s over 20 minutes, which
# is the same size as the matching jitter, so the ordering is genuinely weak. It is
# the large-stretch branch below that can use it.

# --- measured-rate stretch (the large case the discrete ratios cannot reach) ---
# A 2% stretch displaces the end of a 21-minute file by 25s, so the +-8s median trim
# above throws the signal away and the 1001/1000 ratios are the wrong size anyway.
# This branch fits the rate instead, trimming residuals about the LINE so any slope
# survives, and confirms it by flattening: applying the fit must collapse the
# residual spread to healthy-file levels.
# Calibrated THROUGH the pipeline (tests/e2e_matrix.run_one, sampled mode, tiny.en):
# 18 episodes x {clean, uniform, swap, gap, piecewise x4 seeds, fps_late, fps_early,
# drift, drift_swap} = 216 cases. Result: 7 of 36 stretched episodes corrected,
# 0 of 180 others touched.
#
# Measuring OUTSIDE the pipeline gave 77% instead of 19% and was wrong: alass runs
# first and rewrites the file, so the pool we actually see is not the injected error.
# On a 2% stretch alass guesses a false PAL conversion (25/24) and applies it on top,
# turning 2% into 6.25% (measured, C_S03E03) -- which is why the rate cap has to sit
# above PAL, and why most stretched files never produce a fittable pool at all: of 144
# stretch runs, 32 had no line through even 8 anchors and 8 never reached the branch.
STRETCH_RESID_TRIM_S = 1.5
# What counts as "on the line" for keep_frac ONLY (see stretch_probe) -- wider
# than the fit trim above, on purpose. Whisper segment starts routinely sit
# 0.5-1.5s off their cue (see ANCHOR_SUSPECT_THRESHOLD_S), so on a true stretch
# ~10% of anchors fall 1.5-3s off the line: counting them as "off the line"
# put the 0.90 keep bar INSIDE the jitter tail, where dense full-transcript
# pools measured it exactly (0.85-0.89) and failed while sparse sampled pools
# fluctuated around it (0.88-0.96) and passed or failed by luck. The fit itself
# stays at 1.5 -- a wide fit trim lets a competing structure drag the Theil-Sen
# median off the true line (seen on small.en: keep 0.97 but resid 0.41, failing
# the 0.40 bar the wide fit itself inflated). Fit tight, count tolerant.
STRETCH_KEEP_TRIM_S = 2.5
STRETCH_MIN_POINTS = 20
STRETCH_MIN_TILT_S = 8.0      # below this the discrete ratios own the case
STRETCH_MAX_RATE = 0.08       # above PAL+NTSC compounded is a broken pool, not a rate
STRETCH_RHO_MIN = 0.90
STRETCH_MIN_GAIN_S = 1.50
STRETCH_MAX_RESID_S = 0.40
# The load-bearing gate: how much of the pool ONE straight line explains. A stretch
# covers the file end to end; with k blocks a line reaches about 1/k of the anchors.
# Worst block pool measured sits at keep 0.85 (C_S02E09, 6 episodes x 8 seeds x
# both densities) -- under the bar, and it fails the resid gate too (1.01s vs
# 0.40s), so keep is not its only stopper. Matrix piecewise pools sit at
# 0.24-0.63. (An older calibration with 15s clips saw 0.86 on C_S02E12/
# piecewise#3 clearing every other gate; not reproduced at 30s clips -- max
# 0.52 there.) Do not lower this bar to chase drift pools -- the count trim
# (STRETCH_KEEP_TRIM_S) is the jitter-tolerant one; this bar stays strict.
STRETCH_MIN_KEEP_FRAC = 0.90

# Post-rate guard: a correction that fixes the file leaves every quarter quiet.
# True 0.1%/4% fixes peak at 1.0s per quarter; false tilts leave 1.9s or more.
FPS_RESID_MAX_S = 1.5


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


def clip_anchors(segments: list[dict], clip_start_sec: float, subs,
                 window_start_sec: float, window_end_sec: float,
                 ) -> tuple[Optional[dict], list[tuple[float, float]]]:
    """One matching pass, two products: the clip's confident shift estimate (or None)
    plus the RAW (audio_time, subtitle_time) anchor points behind it. The raw points
    feed pooled whole-file fits (framerate tilt) that must see every clip, including
    the 52-57% of 15s clips whose anchors never reach ANCHOR_MIN_COUNT."""
    if not segments:
        return None, []
    matched = _match_segments_to_lines(segments, clip_start_sec, subs,
                                       window_start_sec, window_end_sec)
    points = [(a["segment_start_abs"], a["matched_line_start_sec"]) for a in matched]
    return _robust_clip_shift(matched), points


def _spread(values) -> float:
    """Median absolute deviation -- the agreement measure behind every anchor gate."""
    vals = list(values)
    med = statistics.median(vals)
    return statistics.median(abs(v - med) for v in vals)


def _theil_tilt(pts) -> Optional[float]:
    """Theil-Sen median pairwise slope times the x span. Pure fit, no trimming --
    the caller trims. None when no slope exists. Deterministic (sorted slopes)."""
    pts = sorted((float(x), float(y)) for x, y in pts)
    if len(pts) < 3:
        return None
    slopes = sorted((y2 - y1) / (x2 - x1)
                    for i, (x1, y1) in enumerate(pts)
                    for x2, y2 in pts[i + 1:] if x2 != x1)
    if not slopes:
        return None
    return statistics.median(slopes) * (pts[-1][0] - pts[0][0])


def tilt_from_points(points, trim_s: float, min_points: int, stride: int = 1):
    """Accumulated tilt in seconds over (x, y) points: Theil-Sen median pairwise slope
    times the x span, after trimming |y - median(y)| to trim_s. Returns None when fewer
    than min_points survive to the fit (or no slope exists). Deterministic: slopes are
    sorted before taking the median. Stride thins dense point sets (VAD) to keep the
    O(n^2) pairs tractable; anchor pools are small enough to use whole."""
    pts = sorted((float(x), float(y)) for x, y in points)
    if len(pts) < min_points:
        return None
    med = statistics.median(y for _, y in pts)
    pts = [(x, y) for x, y in pts if abs(y - med) <= trim_s]
    return _theil_tilt(pts[::stride or 1])


def spearman_rho(points) -> Optional[float]:
    """Rank correlation between x and y: +-1 when the offset marches through the file in
    one direction, near 0 when it steps and sits. This is the one statistic that tells a
    rate error from a block error -- a block file's offset is huge but not ordered.
    Ties get average ranks. None below 4 points."""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 4:
        return None

    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        out = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks([x for x, _ in pts]), ranks([y for _, y in pts])
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if den == 0 else num / den


def robust_rate_fit(points, resid_s: float = STRETCH_RESID_TRIM_S,
                    floor: int = 8, iters: int = 3) -> Optional[dict]:
    """Theil-Sen line refitted with residuals trimmed about the LINE, not the median.
    The median trim only works while the tilt is small -- on a 2% stretch it discards
    the very signal it is meant to measure (40 anchors down to 19). Returns
    {"slope", "intercept", "kept", "n", "n_raw"}, or None below `floor` points.

    `floor` is deliberately low: the fit should always produce a reading so the
    caller can gate on HOW MUCH of the pool the line explains (stretch_probe's
    keep_frac), rather than on the fit quietly failing."""
    raw = sorted((float(x), float(y)) for x, y in points)
    if len(raw) < floor:
        return None
    pts = raw
    m = c = None
    for _ in range(max(1, iters)):
        slopes = sorted((y2 - y1) / (x2 - x1)
                        for i, (x1, y1) in enumerate(pts)
                        for x2, y2 in pts[i + 1:] if x2 != x1)
        if not slopes:
            return None
        h = len(slopes) // 2  # already sorted: statistics.median would sort again
        m = slopes[h] if len(slopes) % 2 else (slopes[h - 1] + slopes[h]) / 2
        c = statistics.median(y - m * x for x, y in pts)
        kept = [(x, y) for x, y in raw if abs(y - (m * x + c)) <= resid_s]
        # Too few points near the line means the line does not describe this pool.
        # Returning the untrimmed set instead would report a tight fit over a mess.
        if len(kept) < floor:
            return None
        if kept == pts:
            break
        pts = kept
    return {"slope": m, "intercept": c, "kept": pts, "n": len(pts), "n_raw": len(raw)}


def stretch_probe(points, resid_s: float = STRETCH_RESID_TRIM_S,
                  keep_s: float = STRETCH_KEEP_TRIM_S) -> Optional[dict]:
    """The measured-rate reading of an anchor pool, on (x, y) = (audio time, offset).
    Returns {"slope", "tilt", "rho", "gain", "resid", "keep_frac", "n", "n_raw", "span"}.

    Four independent things a real rate error does, and a block error cannot do all of:

    * tilt       slope x span, seconds -- how far the file has walked by the end
    * rho        monotonicity of the pool (see spearman_rho). A rate error marches;
                 a block error steps and sits. This is what tells them apart --
                 but a SINGLE block in one direction ranks monotone too, so:
    * gain       spread before minus spread after removing the fitted ramp: the
                 flattening test. Remove the rate and a rate error collapses;
                 a block error has no rate to remove.
    * keep_frac  share of the pool the fitted line explains within keep_s. One line
                 covers a rate error end to end; with k blocks it covers about 1/k.

    gain, resid and rho are measured on the SURVIVING points, keep_frac on the whole
    pool -- so a fit that reached tightness by discarding the mess is visible as a low
    keep_frac rather than as a clean-looking fit. keep_s is deliberately wider than
    the fit trim: the fit must be robust (a wide fit trim lets competing structures
    drag it off the line), while the count must tolerate anchor jitter (see
    STRETCH_KEEP_TRIM_S)."""
    fit = robust_rate_fit(points, resid_s)
    if fit is None:
        return None
    kept = fit["kept"]
    span = kept[-1][0] - kept[0][0]
    if span <= 0 or len(kept) < 4:
        return None
    m = fit["slope"]
    before = _spread([y for _, y in kept])
    after = _spread([y - m * x for x, y in kept])
    c = fit["intercept"]
    raw = sorted((float(x), float(y)) for x, y in points)
    on_line = sum(1 for x, y in raw if abs(y - (m * x + c)) <= keep_s)
    return {"slope": m, "intercept": c, "tilt": m * span,
            "rho": spearman_rho(kept), "gain": before - after, "resid": after,
            "n": len(kept), "n_raw": fit["n_raw"],
            "keep_frac": on_line / fit["n_raw"], "span": span}


# A quartile with fewer points abstains: 1-2 stray mismatches in a quarter a steep
# rate emptied vetoed a good fix (one -160s point read as 150s left).
QUARTILE_MIN_POINTS = 5


def max_quartile_residual_after(points, ratio: float, offset: float = 0.0,
                                  nreg: int = 4) -> float:
    """Largest quartile median |offset| left after new_t = ratio * (t + offset).

    `points` are (audio, subtitle) anchor points as pooled by the caller. The
    correction must leave the file quiet EVERYWHERE: a true rate fix collapses
    every quarter to the jitter floor, while a false one either ramps a healthy
    file (late quarters 2-3s) or fits 94% of a stepped pool and leaves the tail
    (5s). Global spread sees neither -- the ramp starts inside the noise and the
    tail is 6% of the pool. 0.0 when unjudgeable (under 4 points, no span)."""
    pts = [(float(a), float(s)) for a, s in points]
    if len(pts) < 4:
        return 0.0
    lo = min(a for a, _ in pts)
    span = max(a for a, _ in pts) - lo
    if span <= 0:
        return 0.0
    regs: list[list[float]] = [[] for _ in range(nreg)]
    for a, s in pts:
        regs[min(nreg - 1, int((a - lo) / span * nreg))].append(a - ratio * (s + offset))
    worst = 0.0
    for r in regs:
        if len(r) >= QUARTILE_MIN_POINTS:
            worst = max(worst, abs(statistics.median(r)))
    return worst


# Whole-file rate on DENSE pools (every matched line of the full transcript, on
# the pre-sync file). SH tiny: healthy/block/hole |tilt| <= 0.4s; 0.1-4.3%
# rates read 2.0-92s at |rho| >= 0.74, keep >= 0.90, gain >= 0.27, resid <= 0.32.
# Blocks: keep <= 0.65 or gain < 0 with resid 0.61. Intercept bias +-0.09s.
RATE_MIN_POINTS = 40
RATE_MIN_TILT_S = 1.5
RATE_MIN_RHO = 0.70
RATE_MIN_KEEP = 0.90
RATE_MIN_GAIN_S = 0.20
RATE_MAX_RESID_S = 0.40
# Flat = nothing left to fix: tilt and offset inside Whisper noise. Tight is
# the healthy SH ceiling (tilt 0.4s, offset 0.09s): a file already fixed
# elsewhere is only left alone when it is that good (p50 0.31s slipped at 1.0).
RATE_FLAT_TILT_S = 1.0
RATE_FLAT_OFFSET_S = 0.25
RATE_TIGHT_TILT_S = 0.5
RATE_TIGHT_OFFSET_S = 0.15
# A block staircase on a ramp reads flat but spreads (healthy SH resid <= 0.28s).
RATE_TIGHT_RESID_S = 0.30
# Real conversions; measured rates land within 0.06 points of them.
RATE_SNAP_RATIOS = ((1000 / 1001, "24/23.976"), (1001 / 1000, "23.976/24"),
                    (24 / 25, "25/24"), (25 / 24, "24/25"),
                    (24000 / 1001 / 25, "25/23.976"), (25 / (24000 / 1001), "23.976/25"))
RATE_SNAP_TOL = 0.0008


def stretch_ratio(p: dict) -> float:
    """new_t = ratio * (t + intercept) undoes a probe's slope."""
    return 1.0 / (1.0 - p["slope"])


def stretch_name(ratio: float) -> str:
    return f"stretch {(ratio - 1) * 100:+.2f}%"


def probe_gates_pass(p: Optional[dict], min_points: int, min_tilt: float, min_rho: float,
                     min_keep: float, min_gain: float, max_resid: float) -> bool:
    """A stretch_probe reading that looks like one whole-file rate, under the given bars."""
    return (p is not None and p.get("rho") is not None and p["n"] >= min_points
            and abs(p["tilt"]) >= min_tilt and abs(p["rho"]) >= min_rho
            and p["keep_frac"] >= min_keep and p["gain"] >= min_gain
            and p["resid"] <= max_resid and abs(p["slope"]) <= STRETCH_MAX_RATE)


def rate_gates_pass(p: Optional[dict]) -> bool:
    return probe_gates_pass(p, RATE_MIN_POINTS, RATE_MIN_TILT_S, RATE_MIN_RHO,
                            RATE_MIN_KEEP, RATE_MIN_GAIN_S, RATE_MAX_RESID_S)


def rate_is_flat(p: Optional[dict], tight: bool = False) -> bool:
    tilt, off = ((RATE_TIGHT_TILT_S, RATE_TIGHT_OFFSET_S) if tight
                 else (RATE_FLAT_TILT_S, RATE_FLAT_OFFSET_S))
    return (p is not None and p["keep_frac"] >= RATE_MIN_KEEP
            and abs(p["tilt"]) < tilt and abs(p["intercept"]) < off
            and (not tight or p["resid"] <= RATE_TIGHT_RESID_S))


def snap_rate(points, p: dict) -> tuple[float, float, str]:
    """(ratio, offset, name) for new_t = ratio * (t + offset) from (audio, cue)
    points. A real conversion ratio wins when it fits the line as well."""
    ratio, off = stretch_ratio(p), p["intercept"]
    line = [(a, s) for a, s in points
            if abs(a - ratio * (s + off)) <= STRETCH_KEEP_TRIM_S] or list(points)

    def mad(r, o):
        return statistics.median(abs(a - r * (s + o)) for a, s in line)

    measured = (mad(ratio, off), ratio, off, stretch_name(ratio))
    snaps = []
    for r, name in RATE_SNAP_RATIOS:
        if abs(r / ratio - 1) <= RATE_SNAP_TOL:
            o = statistics.median(a / r - s for a, s in line)
            snaps.append((mad(r, o), r, o, name))
    best = min(snaps, default=measured)
    if best[0] > measured[0] + 0.02:
        best = measured
    return best[1], best[2], best[3]


def anchor_drift_signature(points, trim_s: float = FPS_ANCHOR_TRIM_S,
                           min_points: int = FPS_MIN_ANCHORS,
                           nbins: int = FPS_BINS, min_votes: int = FPS_MIN_VOTES,
                           loo_parts: int = FPS_LOO_PARTS) -> Optional[dict]:
    """The three anchor statistics behind the framerate decision, measured on ONE
    trimmed pool (None when fewer than min_points survive the trim). Returns
    {"tilt", "binned", "drops", "n"}:

    * tilt: Theil-Sen slope times span over all points (magnitude).
    * binned: the same fit over per-region median votes (one vote per 1/16 of the
      span -- a bad clip holding 30% of the points is only 1-2 votes here).
    * drops: the tilt with each 1/8 x-range part left out (redundancy: a global
      drift survives dropping any eighth; a tilt hinged on one region collapses).
    * rho: monotonicity (spearman_rho). A rate error walks the offset steadily
      later and later through the file; a block error steps and sits. This is the
      one statistic that separates the two -- the other three measure HOW MUCH the
      pool tilts, not whether the tilt is ordered.

    Why three: sampled anchor pools are noisy (matching jitter plus 1-2 bad clips
    per file), and EVERY single statistic misfires on some healthy file -- plain
    tilt on C_S02E13 (+0.87, concentrated bad early clip), binned on C_S03E12
    (+0.86, noisy 2-point bin medians), thirds/inliers/Spearman all overlap too.
    The AND of the three separates 39/39 measured pools (4 drifting files in both
    modes pass; all 31 others veto somewhere). VAD confirmation still applies on
    top (see vad_tilt_from_intervals); sampled evidence only ever TRIGGERS a
    full-transcript confirmation, it never fixes (see pipeline._try_fps_rescale).
    """
    pts = sorted((float(x), float(y)) for x, y in points)
    if len(pts) < min_points:
        return None
    med = statistics.median(y for _, y in pts)
    pts = [(x, y) for x, y in pts if abs(y - med) <= trim_s]
    if len(pts) < min_points:
        return None
    tilt = _theil_tilt(pts)
    lo, hi = pts[0][0], pts[-1][0]
    span = hi - lo
    binned = None
    if span > 0:
        bins: list[list[float]] = [[] for _ in range(nbins)]
        for x, y in pts:
            bins[min(nbins - 1, int((x - lo) / span * nbins))].append(y)
        votes = [(lo + (i + 0.5) / nbins * span, statistics.median(b))
                 for i, b in enumerate(bins) if b]
        if len(votes) >= min_votes:
            binned = _theil_tilt(votes)
    drops: list[Optional[float]] = []
    if span > 0:
        w = span / loo_parts
        for j in range(loo_parts):
            rest = [(x, y) for x, y in pts
                    if not (lo + j * w <= x < lo + (j + 1) * w
                            or (j == loo_parts - 1 and x == hi))]
            drops.append(_theil_tilt(rest) if len(rest) >= min_points else None)
    else:
        drops = [None] * loo_parts
    return {"tilt": tilt, "binned": binned, "drops": drops, "n": len(pts),
            "rho": spearman_rho(pts)}


def vad_tilt_from_intervals(subs, intervals):
    """Framerate tilt from speech-onset alignment: for every cue start, the nearest
    speech onset within FPS_VAD_MAXGAP_S, trimmed and Theil-Sen fitted like anchors.
    Returns (tilt_or_None, n_points_after_trim). Pure function of its inputs -- the
    caller (pipeline) supplies whatever timeline is available (clip cache, full
    transcript cache, or a Silero run; see vad.timeline_for_video).

    The matching runs iteratively (up to 3 median-prefetch rounds): plain
    nearest-onset matching is fragile under global cue shifts -- measured: alass's
    own perfect rescale of an injected ramp left a constant +0.14s residual, and
    that alone flipped this estimator +0.01s -> +0.84s on sparse clip-cache onsets
    (cues near onset midpoints rematch across the gap in bursts). The prefetch
    absorbs the global shift before the tilt fit sees it; on real drift it
    converges toward the mid-file offset instead, which IMPROVES the matching
    (large drift otherwise mismatches cues onto neighbors' onsets -- dense VAD on
    C_S03E04 goes from a washed-out +0.04 to -0.71). The remaining sparse-mode
    fragility is why this estimator only ever CONFIRMS on full-transcript
    evidence behind the anchor gates (see pipeline._try_fps_rescale), never
    decides on its own.
    """
    onsets = sorted(a for a, _ in (intervals or []))
    if not onsets:
        return None, 0
    starts = [e.start / 1000.0 for e in subs.events]

    def match(offset):
        pts = []
        for t in starts:
            i = bisect.bisect_left(onsets, t + offset)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(onsets):
                    d = onsets[j] - t
                    if abs(d - offset) <= FPS_VAD_MAXGAP_S and (
                            best is None or abs(d - offset) < abs(best - offset)):
                        best = d
            if best is not None:
                pts.append((t, best))
        return pts

    pts = match(0.0)
    if len(pts) < FPS_VAD_MIN_POINTS:
        return None, len(pts)
    offset = 0.0
    for _ in range(3):
        med = statistics.median(y for _, y in pts)
        if abs(med - offset) < 0.05:
            break
        offset = med
        pts = match(offset)
        if len(pts) < FPS_VAD_MIN_POINTS:
            return None, len(pts)
    med = statistics.median(y for _, y in pts)
    kept = [(x, y) for x, y in pts if abs(y - med) <= FPS_VAD_TRIM_S]
    if len(kept) < FPS_VAD_MIN_POINTS:
        return None, len(kept)
    return tilt_from_points(kept, FPS_VAD_TRIM_S, FPS_VAD_MIN_POINTS, stride=3), len(kept)


def apply_fps_rescale(subs, ratio: float, offset: float = 0.0) -> float:
    """Retimes every cue in place as new_t = ratio * (t + offset).

    The framerate path passes offset = 0: the pivot is the file start, which is where
    a framerate mismatch pivots, and that restores the original timing -- cue lead
    included -- exactly. A median-preserving offset instead bakes the ramp's own median
    (0.001 * t_median, +0.63s on 21 min) in as a permanent global shift (measured on
    synthetic round-trips).

    The measured-rate path passes the FITTED intercept, because there the file is not
    pivoting at zero: alass has already shifted it, so the pool reads a rate AND an
    offset, and undoing only the rate leaves the shift behind (measured: 19s left on
    C_S02E01). Solving a - s = m*a + c for a gives a = (s + c) / (1 - m) -- ratio and
    offset together are that inverse.

    Returns the largest absolute cue change in seconds. Clamps at zero like
    apply_anchor_resync."""
    off_ms = offset * 1000.0
    worst = 0.0
    for e in subs.events:
        new_start = max(0, int(round((e.start + off_ms) * ratio)))
        new_end = max(new_start, int(round((e.end + off_ms) * ratio)))
        worst = max(worst, abs(new_start - e.start) / 1000.0, abs(new_end - e.end) / 1000.0)
        e.start, e.end = new_start, new_end
    return worst


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
