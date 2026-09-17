"""Detects two-line subtitle entries where the two lines are in the wrong (reversed) reading
order, e.g. line 2 is actually spoken before line 1. Auto-fixing/flagging is off unless
sync.line_order_enabled is on (see Settings -> Automation's "What runs" table), but the two
layers below actually run — and get cached — every time a correctness check does, whether or not
line-order is on (see pipeline.py): there's no reason not to, since layer 2 rides on the same
Whisper clips correctness is already sending.

Two layers, cheapest first:

1. A free, local heuristic (_cap_signal): line 1 starts lowercase AND line 2 starts uppercase,
   UNLESS line 1 ends in ./!/? (then it's a complete sentence on its own — L2 starting a new
   capitalized sentence right after it is normal, not a swap signal). Used as a Whisper-cost
   pre-filter in sampled mode (collect_samples) and for line_flagged's "unconfirmed, worth a
   human look" report in both modes. NOT used to gate which candidates full mode's line_issues
   auto-fixes from — see collect_samples_full and _has_bridging_segment.

2. Whisper audio confirmation, via collect_samples() — ONE sampling pass, sized exactly like
   correctness_check's own sample count, not additive to it. Each sampled clip is placed AT a
   heuristic candidate's timestamp when one is available (so that clip judges swap order too, for
   free), and at correctness_check's usual spread-out points otherwise. Every clip's transcript
   feeds the ordinary correctness overlap score regardless of why it was picked — so "is this
   even the right subtitle for this episode" is answered by the same multi-sample majority-vote
   correctness_check already uses everywhere else (see _aggregate_correctness in correctness.py),
   not by a single clip. This replaces running correctness_check separately (two separate
   sampling passes meant a file with few/no heuristic hits paid for both). The
   result is cached (cache_key_for, keyed on the subtitle's own content) so a later run against an
   unchanged subtitle reuses it instead of re-transcribing.

Per-line auto-fix (apply_line_swap) and the "widespread pattern" note (finalize_line_order's
swap_severity) are BOTH based on layer 2 alone, for every TESTED candidate it confirms --
swapping two lines back is a safe, mechanical fix regardless of how many lines are affected, and
neither of these blocks/redownloads the file (a real content problem is caught separately, by the
ordinary correctness score -- see pipeline.py). An LLM opinion used to gate the widespread note
(the two lines' TEXT alone, no audio/transcript); dropped after checking it against real
fixtures -- Whisper's own per-candidate verdict, inspected directly against the actual audio
transcript (e.g. displayed "...to be popular with women, / I know you're excited" vs. what was
actually said: "...I know you're excited to finally be popular with women..."), was not noise,
it was catching genuine pre-existing swaps a second, weaker, ungrounded opinion added nothing to."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import NamedTuple, Optional

from verifyarr import log
from verifyarr import db
from verifyarr import vad
from verifyarr import generate
from verifyarr.correctness import (JobCancelled, _aggregate_correctness, _compare_transcript_to_window,
                                    detect_audio_language_ffprobe, extract_clip,
                                    get_duration_seconds, transcribe_verbose)
from verifyarr.settings import Config
from verifyarr.subtitles import (
    pick_dialogue_dense_time, subs_fingerprint, subs_text_in_window, tokenize,
    clip_anchor_shift, anchors_applicable,
)

# Padding either side of a candidate's own [start, end] for AUDIO EXTRACTION only (not for
# judging which segments belong to the candidate — see _cluster_windows).
PAD_SECONDS = 2.0

# Candidates whose padded windows are within this many seconds of each other share ONE audio
# extraction + ONE Whisper call instead of one each.
MERGE_GAP_SECONDS = 20.0

# Hard cap on a single merged clip's length, even if many candidates chain together.
MAX_CLIP_SECONDS = 240.0

# Minimum score gap (0-1, token-overlap fraction) between "swapped order fits better" and
# "displayed order fits better" before the audio is treated as decisive either way.
SWAP_MARGIN = 0.15

# Same idea as SWAP_MARGIN but for the difflib.SequenceMatcher fallback (_sequence_order_verdict)
# used when there aren't 2+ segments to split by timing. Validated against the real swap that
# motivated this fallback (S03E01 #69: displayed 0.56 vs swapped 1.0, a 0.44 gap).
SEQUENCE_MARGIN = 0.1



def _split_two_lines(text: str) -> Optional[tuple[str, str]]:
    lines = [l for l in text.replace("\\N", "\n").split("\n") if l.strip()]
    if len(lines) != 2:
        return None
    return lines[0], lines[1]


def _cap_signal(l1: str, l2: str) -> bool:
    """L1 starts lowercase AND L2 starts uppercase — UNLESS L1 ends in ./!/? , in which case L1
    is a complete sentence in its own right and L2 starting a new, capitalized sentence right
    after it is completely normal, not a swap signal."""
    if not l1 or not l2 or not l1[0].islower() or not l2[0].isupper():
        return False
    return l1.rstrip()[-1:] not in ".!?"


def _iter_two_line_events(subs):
    """Every 2-line subtitle event as (index, l1, l2, start_ms, end_ms), in subtitle order —
    the single scan behind both heuristic_candidates (flagged only) and all_two_line_events
    (everything), so the two can't disagree on what "a 2-line event" is."""
    for i, e in enumerate(subs.events):
        split = _split_two_lines(e.text)
        if split:
            yield i, split[0], split[1], e.start, e.end


def heuristic_candidates(subs) -> list[tuple[int, str, str, int, int]]:
    """Every 2-line event where _cap_signal's free, local heuristic flags a possible reversed
    line order — a candidate PRE-FILTER, never a verdict on its own (see module docstring).
    (index, l1, l2, start_ms, end_ms), in subtitle order."""
    return [(i, l1, l2, start, end) for i, l1, l2, start, end in _iter_two_line_events(subs)
            if _cap_signal(l1, l2)]


def all_two_line_events(subs) -> list[tuple[int, str, str, int, int]]:
    """Every 2-line event, no pre-filter. Only safe to test with _judge_order's bridging-segment
    guard in place (see module docstring) -- without it this is noise, not detections."""
    return list(_iter_two_line_events(subs))


def cache_key_for(subs, cfg: Config) -> str:
    """Identifies a `collect_samples` result as still valid for THIS subtitle content under
    THESE settings (see pipeline.py) — a subtitle's fingerprint (content, not file mtime/size)
    plus every setting that changes which clips get picked/how they're judged. Any change to the
    subtitle or these settings naturally produces a different key, so a stale cache is never
    reused by accident. whisper_mode included so switching sampled/full re-collects rather than
    reusing the other mode's cached samples."""
    return f"{subs_fingerprint(subs)}:{cfg.sample_count}:{cfg.clip_seconds}:{cfg.window_minutes}:{cfg.whisper_mode}"


def _window_subtitle_text(subs, start_sec: float, end_sec: float) -> str:
    """Every subtitle line displayed within [start_sec, end_sec] — not just the 2-line
    candidates a cluster happens to contain, so the correctness comparison sees the same kind of
    "what does the subtitle claim is being said here" text correctness_check compares against."""
    lo_ms, hi_ms = start_sec * 1000, end_sec * 1000
    return "\n".join(e.plaintext for e in subs.events if lo_ms <= e.start <= hi_ms)


class _PaddedWindow(NamedTuple):
    """One candidate's padded extraction window: index/l1/l2 identify the candidate,
    raw_start/raw_end are its ACTUAL unpadded subtitle timing (what _judge_order matches
    Whisper segments against — using the padded window there previously pulled in
    neighboring segments from adjacent dialogue and broke the segment-count/
    sequence-match logic, found while debugging a real miss), win_start/win_end are the
    padded outer bounds (what actually gets extracted, and what merging decides on)."""
    index: int
    l1: str
    l2: str
    raw_start: float
    raw_end: float
    win_start: float
    win_end: float


def _cluster_windows(candidates: list[tuple[int, str, str, int, int]]) -> list[dict]:
    """candidates: (index, l1, l2, start_ms, end_ms). Returns clusters, sorted by time, each
    {"clip_start": sec, "clip_end": sec, "items": [(index, l1, l2, raw_start_sec, raw_end_sec)]}."""
    windows = sorted(
        (_PaddedWindow(i, l1, l2, s / 1000.0, e / 1000.0,
                       max(0.0, s / 1000.0 - PAD_SECONDS), e / 1000.0 + PAD_SECONDS)
         for i, l1, l2, s, e in candidates),
        key=lambda w: w.win_start,
    )
    clusters: list[dict] = []
    for w in windows:
        item = (w.index, w.l1, w.l2, w.raw_start, w.raw_end)
        if (clusters and w.win_start - clusters[-1]["clip_end"] <= MERGE_GAP_SECONDS
                and w.win_end - clusters[-1]["clip_start"] <= MAX_CLIP_SECONDS):
            clusters[-1]["items"].append(item)
            clusters[-1]["clip_end"] = max(clusters[-1]["clip_end"], w.win_end)
        else:
            clusters.append({"clip_start": w.win_start, "clip_end": w.win_end, "items": [item]})
    return clusters


def _normalize_for_sequence_match(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower())


def _sequence_order_verdict(transcript_text: str, l1: str, l2: str) -> Optional[bool]:
    """Fallback for when segment TIMING can't split "said first" from "said second" (see
    _judge_order) — typically a fast utterance Whisper transcribed as one undivided segment. A
    plain word-overlap score can't tell order apart either (swapping two lines doesn't change
    which words are used, just their sequence), but a full continuous phrase in the WRONG order
    reads nothing like the actual transcript's word sequence, so a fuzzy sequence match (difflib,
    stdlib) against each full hypothesis ("L1 L2" vs "L2 L1") can still tell them apart."""
    t = _normalize_for_sequence_match(transcript_text)
    if not t.strip():
        return None
    # Each line needs at least one of its own words to actually occur SOMEWHERE in the
    # transcript -- a line whose vocabulary never appears at all (Whisper never captured it, or
    # the SRT paraphrases it) gives no real evidence for its position, just an arbitrary default.
    t_tokens = tokenize(transcript_text)
    if not (tokenize(l1) & t_tokens) or not (tokenize(l2) & t_tokens):
        return None
    displayed = _normalize_for_sequence_match(f"{l1} {l2}")
    swapped = _normalize_for_sequence_match(f"{l2} {l1}")
    displayed_score = difflib.SequenceMatcher(None, t, displayed).ratio()
    swapped_score = difflib.SequenceMatcher(None, t, swapped).ratio()
    if swapped_score - displayed_score >= SEQUENCE_MARGIN:
        return True
    if displayed_score - swapped_score >= SEQUENCE_MARGIN:
        return False
    return None


def _has_bridging_segment(overlapping: list[dict], l1_tok: set[str], l2_tok: set[str]) -> bool:
    """True if a SINGLE Whisper segment (Whisper's own sentence, not our first/second-half split)
    contains words from BOTH lines -- i.e. Whisper itself heard this as one continuous utterance
    straddling the line break, not two separately-punctuated sentences. Without this, two
    independent complete sentences that each cleanly match one line, just in reverse order, look
    identical to a genuinely broken sentence (found via 5 real false positives, e.g. "No wrong
    answers." / "I like beer." -- two different speakers' own separate, accurately-transcribed
    lines)."""
    return any((st := tokenize(s["text"])) & l1_tok and st & l2_tok for s in overlapping)


def _dash_prefixed(line: str) -> bool:
    """A leading '-' is the standard SRT/HI convention for "a different speaker than the line
    above" -- two dash-prefixed lines in one block are two people's own separate utterances, not
    one sentence broken across the line break, so there's no well-defined "correct order" here
    for this algorithm to find (found via a real false positive, Slow Horses S01E02 #715: '- ...
    which is none of my business.' / '- No. River, it is your business. I...' -- each half is
    itself a fragment of a DIFFERENT longer sentence continuing into the block before/after)."""
    return line.lstrip().startswith("-")


def _segment_midpoint(segment: dict) -> float:
    """A Whisper segment's temporal center — the single point deciding which half of a
    candidate window (or whether the window at all) the segment belongs to."""
    return (segment["start"] + segment["end"]) / 2


def _judge_order(segments: list[dict], rel_start: float, rel_end: float, l1: str, l2: str) -> Optional[bool]:
    """True = audio confirms L2-then-L1 (swapped) fits better. False = audio confirms the
    displayed L1-then-L2 order fits better. None = inconclusive."""
    if _dash_prefixed(l1) or _dash_prefixed(l2):
        return None
    # Midpoint-inside-window, not "touches the window at all" — a segment that only grazes the
    # boundary must NOT count as belonging to this candidate (found in testing, S03E01 #69).
    overlapping = sorted((s for s in segments if rel_start <= _segment_midpoint(s) <= rel_end),
                          key=lambda s: s["start"])
    if len(overlapping) < 2:
        return _sequence_order_verdict(" ".join(s["text"] for s in overlapping), l1, l2)
    mid = (rel_start + rel_end) / 2
    first_text = " ".join(s["text"] for s in overlapping if _segment_midpoint(s) < mid)
    second_text = " ".join(s["text"] for s in overlapping if _segment_midpoint(s) >= mid)
    if not first_text.strip() or not second_text.strip():
        return None

    t_first, t_second = tokenize(first_text), tokenize(second_text)
    l1_tok, l2_tok = tokenize(l1), tokenize(l2)
    # A word said on BOTH sides of the split carries no positional information at all (it doesn't
    # distinguish "this is L1's half" from "this is L2's half"), so it can't count as evidence for
    # either line -- found via a real false positive (Slow Horses S01E02 #164: "we're" recurs in
    # both "We're just slow horses" and "We're grunts", and being l1's ONLY token was enough to
    # flip the verdict). Dynamic, not a hand-maintained word list: catches whichever word happens
    # to repeat, not just ones already known to cause trouble.
    repeated = t_first & t_second
    t_first, t_second = t_first - repeated, t_second - repeated
    # Same idea, other direction: a word in BOTH lines' own displayed text can't tell them apart
    # either -- if it's then heard on just one side of the audio, that's no more evidence it's
    # THIS line's half than the other line's (either could have said it). Found via Slow Horses
    # S01E02 #196: both lines happen to start with "It's" ("It's for a good cause." / "It's my
    # daughter's school."), and only one side's "it's" survived the split by chance.
    line_shared = l1_tok & l2_tok
    l1_tok, l2_tok = l1_tok - line_shared, l2_tok - line_shared
    # Each line needs at least one of its own words to actually occur SOMEWHERE in this window --
    # a line whose vocabulary never appears at all (Whisper never captured it, or the SRT
    # paraphrases it) gives no real evidence for its position; the overlap score below would
    # otherwise be decided entirely by the OTHER line's match, with nothing to weigh it against.
    both = t_first | t_second
    if not (l1_tok & both) or not (l2_tok & both):
        return None
    if not _has_bridging_segment(overlapping, l1_tok, l2_tok):
        return None

    def overlap(transcript_tokens: set[str], line_tokens: set[str]) -> float:
        return (len(transcript_tokens & line_tokens) / len(line_tokens)) if line_tokens else 0.0

    displayed_score = overlap(t_first, l1_tok) + overlap(t_second, l2_tok)
    swapped_score = overlap(t_first, l2_tok) + overlap(t_second, l1_tok)
    if swapped_score - displayed_score >= SWAP_MARGIN:
        return True
    if displayed_score - swapped_score >= SWAP_MARGIN:
        return False
    return None


def _rate(verdicts: dict[int, Optional[bool]]) -> tuple[int, int]:
    """(confirmed, checked). checked EXCLUDES inconclusive (None) results entirely — an
    inconclusive candidate is neither evidence of a swap nor evidence against one, so it must
    shrink the sample, not silently count toward "not swapped"."""
    confirmed = sum(1 for v in verdicts.values() if v is True)
    checked = sum(1 for v in verdicts.values() if v is not None)
    return confirmed, checked


def _meets_swap_threshold(confirmed: int, checked: int, cfg: Config) -> bool:
    if checked == 0 or confirmed < cfg.line_order_swap_threshold_min:
        return False
    return confirmed / checked >= cfg.line_order_swap_threshold_pct


def _covered_positions(slots: list) -> list[float]:
    """Clip-start positions that actually sampled audio. A VAD silence-skip
    (a None filler/extra slot, see collect_samples) covers nothing -- no audio
    was even listened to there -- so a block whose only slot is silence still
    gets its extra targeted sample instead of crashing the range comparison
    (None is not orderable against a float range bound)."""
    return [slot["clip_start"] if kind == "heuristic" else slot
            for kind, slot, _ in slots
            if kind == "heuristic" or slot is not None]


def _extract_and_transcribe(video_path: Path, start_sec: float, duration_sec: float, cfg: Config,
                             audio_lang: Optional[str], tmp_dir: Path, cancel_event=None) -> Optional[dict]:
    """Extract one clip and transcribe it via correctness.transcribe_verbose -- same cloud
    fallback-on-429 policy, and respects use_local_whisper (this used to call _transcribe_once
    directly, which always hit the cloud API regardless of that setting). Returns the parsed
    response, or None if extraction or transcription failed."""
    clip_path = tmp_dir / f"clip_{int(start_sec)}.wav"
    try:
        if not extract_clip(video_path, start_sec, round(duration_sec, 1), clip_path):
            log.warning("check_subtitle: could not extract clip at %.1fs for %s", start_sec, video_path.name)
            return None
        try:
            return transcribe_verbose(cfg, clip_path, audio_lang, cancel_event=cancel_event)
        except JobCancelled:
            raise
        except Exception as e:
            log.warning("check_subtitle: transcription failed for %s: %s", video_path.name, e)
            return None
    finally:
        clip_path.unlink(missing_ok=True)


def collect_samples(video_path: Path, subs, sub_lang: Optional[str], cfg: Config, tmp_dir: Path,
                     conn=None, cancel_event=None,
                     extra_target_ranges: Optional[list[tuple[float, float]]] = None) -> dict:
    """The Whisper-spending half of the combined "is this subtitle correct, and are any lines
    swapped" check. ONE sampling pass, sized exactly like correctness_check's own sample_count
    (not additive to it, and not longer for a movie than a series).

    duration is divided into n equal-width regions, spread evenly across the whole file. Each
    region contributes at most one clip: the first heuristic candidate cluster that starts in it,
    if there is one (so that clip does double duty — it also gets its line order judged), or
    otherwise the most dialogue-dense point in that region (pick_dialogue_dense_time) instead of
    a blind timestamp, so a sample doesn't land on a silent or action-heavy stretch with nothing
    to compare. This also naturally keeps samples spread out even on a file with many heuristic
    hits clustered in one act, or none at all.

    Run whenever a correctness check runs at all (pipeline.py), regardless of whether the
    line-order feature is even turned on — the clips are already being sent to Whisper for
    correctness, so judging their line order too costs nothing extra. Returns a dict fed to
    finalize_line_order() below, and JSON-serializable (JSON keys aside — see pipeline.py) so it
    can be cached across runs, keyed on cache_key_for(): a later run with an unchanged subtitle
    reuses this instead of re-transcribing.

    Returns {"skipped": True, "reason": ...} or {"skipped": False, "samples", "audio_lang",
    "whisper_verdicts": {index: True/False/None}, "tested_items": [(index, l1, l2), ...],
    "candidates": [(index, l1, l2, start_ms, end_ms), ...]}.

    conn: optional sqlite3 connection, passed straight through to video_transcript_cache (see
    correctness.correctness_check's own conn param — same table, same reasoning: the audio at
    a given point doesn't depend on which subtitle is checking it). Cache HITS only apply to
    "filler"/"extra" slots (plain dialogue-dense points) — a "heuristic" slot is anchored to
    THIS subtitle's own claimed line timing (that's the whole point of judging its order), so
    it's always transcribed fresh regardless of conn; its transcript is still SAVED, keyed by
    position, as audio evidence for later candidate comparisons of the same video.

    extra_target_ranges: optional [(start_sec, end_sec), ...] -- e.g. alass block boundaries a
    suspicious multi-block fix needs individually verified (see pipeline.sync_pair/_block_time_
    ranges). Each range not already covered by one of the n normal, evenly-spread slots gets ONE
    extra forced filler slot of its own, appended after the normal n. Cached by POSITION, not by
    slot number (db.find_cached_transcript_between / db.extra_slot_index): what makes such a
    sample reusable is that it lies inside the block, and a plain "slot n" key silently
    re-pointed a later run's targeted sample at wherever an earlier run's slot n had landed.
    Without any of this, a file with more structural blocks than cfg.sample_count is
    guaranteed to leave at least one block completely unchecked by pure chance of where the n
    regions happened to fall."""
    candidates = heuristic_candidates(subs)

    duration = get_duration_seconds(video_path)
    if not duration:
        return {"skipped": True, "reason": "could not read duration (ffprobe)"}
    audio_lang = detect_audio_language_ffprobe(video_path)
    if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
        reason = f"speech is '{audio_lang}' (per the file's metadata), not '{cfg.require_audio_lang}' — skipped"
        return {"skipped": True, "reason": reason}
    lang = audio_lang or cfg.require_audio_lang

    n = max(1, cfg.sample_count)
    regions = [(duration * i / n, duration * (i + 1) / n) for i in range(n)]
    clusters = _cluster_windows(candidates) if candidates else []
    # Speech timeline for VAD-guided filler placement (see vad.py; None keeps today's
    # dialogue-density positions bit-for-bit). Heuristic slots stay anchored to this
    # subtitle's own timing and are never moved.
    timeline = vad.timeline_for_video(conn, video_path, cfg)

    # One slot per region: ("heuristic", cluster, None) if a candidate cluster starts in it,
    # else ("filler", start_sec, (region_start, region_end)) at that region's most dialogue-
    # dense point. A cluster only ever starts in exactly one region, so no cluster can be picked
    # twice here. The bounds travel with the slot so a cache hit can be checked against them.
    slots: list[tuple[str, object, Optional[tuple[float, float]]]] = []
    for region_start, region_end in regions:
        in_region = next((c for c in clusters if region_start <= c["clip_start"] < region_end), None)
        if in_region is not None:
            slots.append(("heuristic", in_region, None))
        else:
            base = pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)
            start = vad.pick_sample_time(subs, timeline, region_start, region_end,
                                         cfg.clip_seconds, base, cfg.vad_min_speech_seconds)
            slots.append(("filler", start, (region_start, region_end)))

    if extra_target_ranges:
        # A slot's own chosen position (its clip's start) is what "covers" a target range, not
        # the region boundaries -- a region can span a target range's edge without its clip
        # actually landing inside it.
        covered_positions = _covered_positions(slots)
        for range_start, range_end in extra_target_ranges:
            if any(range_start <= pos < range_end for pos in covered_positions):
                continue
            # TWO samples per uncovered block, not one: a single clip is not enough to
            # characterize a whole block, which can span many minutes -- verified against a
            # real file where the block's only sampled clip happened to land in one anomalous
            # ~25s stretch (a repeated short exchange) unrepresentative of the other ~10
            # minutes around it, and that one clip alone decided pipeline._resolve_ambiguous_
            # sync's verdict for the entire block. Splitting the range in half and picking the
            # dialogue-densest point in EACH half spreads the two samples apart instead of
            # letting them cluster in the same few seconds; if the range is too short for two
            # meaningfully distinct clips, the second is skipped (see the dedup below).
            mid = (range_start + range_end) / 2
            b1 = pick_dialogue_dense_time(subs, range_start, mid, cfg.clip_seconds)
            b2 = pick_dialogue_dense_time(subs, mid, range_end, cfg.clip_seconds)
            p1 = vad.pick_sample_time(subs, timeline, range_start, mid,
                                      cfg.clip_seconds, b1, cfg.vad_min_speech_seconds)
            p2 = vad.pick_sample_time(subs, timeline, mid, range_end,
                                      cfg.clip_seconds, b2, cfg.vad_min_speech_seconds)
            if p1 is not None:
                slots.append(("extra", p1, (range_start, range_end)))
            if p2 is not None and (p1 is None or abs(p2 - p1) >= cfg.clip_seconds):
                slots.append(("extra", p2, (range_start, range_end)))

    samples: list[dict] = []
    whisper_verdicts: dict[int, Optional[bool]] = {i: None for i, *_ in candidates}
    tested_items: list[tuple[int, str, str]] = []  # (index, l1, l2) actually sent to Whisper

    def _run_clip(start_sec: float, clip_duration: float) -> Optional[dict]:
        nonlocal audio_lang, lang
        result = _extract_and_transcribe(video_path, start_sec, clip_duration, cfg,
                                          audio_lang, tmp_dir, cancel_event=cancel_event)
        if result is None:
            return None
        if audio_lang is None:
            audio_lang = result.get("language")
            lang = audio_lang or cfg.require_audio_lang
        return result

    window_before = cfg.window_minutes * 60
    window_after = cfg.clip_seconds + cfg.window_minutes * 60

    for idx, (kind, slot, bounds) in enumerate(slots):
        cached = None
        cache_index = None
        if kind == "heuristic":
            cluster = slot
            start, clip_duration = cluster["clip_start"], cluster["clip_end"] - cluster["clip_start"]
        else:
            if slot is None:
                # VAD-proven silence: non-evidence at the region middle, no STT call (same
                # {"start", "error"} shape extraction failures produce; anchor stays None).
                mid = (bounds[0] + bounds[1]) / 2 if bounds else 0.0
                samples.append({"start": round(mid, 1),
                                "error": f"VAD silence-skip (<{cfg.vad_min_speech_seconds:g}s speech in window)",
                                "anchor": None})
                continue
            start, clip_duration = slot, cfg.clip_seconds
            # Filler/extra slots (not heuristic -- see collect_samples' docstring) share the SAME
            # video-level cache correctness_check uses: the audio doesn't depend on which
            # subtitle is checking it, so an earlier check of this video (any subtitle, either
            # code path) may already have transcribed a usable clip. A normal slot is looked up
            # by its slot number, validated against this run's region for it (see
            # db.get_cached_transcript's `within`); an extra slot by position alone.
            if conn is not None and kind == "filler":
                cache_index = idx
                cached = db.get_cached_transcript(conn, video_path, idx, within=bounds)
            elif conn is not None:
                cached = db.find_cached_transcript_between(conn, video_path, bounds[0], bounds[1])

        if cached is not None:
            start = cached["start"]
            transcript_text = cached["transcript"]
            # A row cached before segments_json existed has segments=None -- anchors simply
            # aren't computable from a cache hit like that, same as any other insufficient-data
            # case (see subtitles._robust_clip_shift).
            segments = cached.get("segments") or []
            if audio_lang is None:
                audio_lang = cached["audio_lang"]
                lang = audio_lang or cfg.require_audio_lang
        else:
            result = _run_clip(start, clip_duration)
            if result is None:
                samples.append({"start": round(start, 1), "error": "audio extraction/transcription failed"})
                continue
            if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
                return {"skipped": True, "reason": f"speech is '{audio_lang}', not '{cfg.require_audio_lang}' — skipped"}
            segments = result.get("segments") or []
            transcript_text = " ".join(s.get("text", "") for s in segments)
            if conn is not None:
                # Every fresh clip goes into the video-level cache, heuristic ones included: the
                # audio at that position is just as valid evidence for judging OTHER sync
                # candidates of this video (correctness.evaluate_against_cached_transcripts) as a
                # filler clip is. Only the n normal slots are keyed by slot number; everything
                # else by position (db.extra_slot_index), and a heuristic slot itself is still
                # always transcribed fresh (its cluster is specific to this subtitle's timing).
                if cache_index is None:
                    cache_index = db.extra_slot_index(start)
                db.save_transcript_cache(conn, video_path, cache_index, start, audio_lang, transcript_text,
                                          segments=segments, clip_seconds=clip_duration)

        if kind == "heuristic":
            window_text = _window_subtitle_text(subs, cluster["clip_start"], cluster["clip_end"])
            for i, l1, l2, raw_start, raw_end in cluster["items"]:
                rel_start, rel_end = raw_start - cluster["clip_start"], raw_end - cluster["clip_start"]
                whisper_verdicts[i] = _judge_order(segments, rel_start, rel_end, l1, l2)
                tested_items.append((i, l1, l2))
            anchor_window = (cluster["clip_start"], cluster["clip_end"])
        else:
            window_text = subs_text_in_window(subs, start, window_before, window_after)
            anchor_window = (start - window_before, start + window_after)

        # Anchors are computed whenever segments exist (CPU only, and cached with the sample so
        # a later run never has to redo it); whether anything ACTS on them is a separate
        # decision (Config.anchor_check_enabled, pipeline._resolve_ambiguous_sync). Never for a
        # subtitle in another language than the audio -- see subtitles.anchors_applicable.
        anchor_info: Optional[dict] = None
        if segments and anchors_applicable(sub_lang, lang):
            anchor_info = clip_anchor_shift(segments, start, subs, anchor_window[0], anchor_window[1])

        compare = _compare_transcript_to_window(cfg, transcript_text, window_text, sub_lang, lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": round(start, 1), "error": compare["error"], "anchor": anchor_info})
        else:
            samples.append({"start": round(start, 1), "anchor": anchor_info, **compare})

    return {"skipped": False, "samples": samples, "audio_lang": audio_lang,
            "whisper_verdicts": whisper_verdicts, "tested_items": tested_items, "candidates": candidates,
            "heuristic_indices": [i for i, *_ in candidates]}


# sync.whisper_mode == "full" samples FULL_MODE_SAMPLE_MULTIPLIER times sample_count's usual
# window count -- dense coverage is free here (the transcript already covers the whole file), but
# window count still bounds LLM-translation cost for a subtitle in another language than the
# audio (one _compare_transcript_to_window call per window) -- a multiplier, not "every clip_seconds".
FULL_MODE_SAMPLE_MULTIPLIER = 5

# Separate from the above: a FIXED-interval anchor-only pass, added below regardless of file
# length -- anchors cost nothing (same-language token overlap, no LLM/API call, see
# subtitles.clip_anchor_shift), so unlike the content-scored windows above there's no reason to
# cap their count. A fixed WINDOW COUNT dilutes with file length (a 2.5h movie gets the same total
# as a 20min episode); a fixed INTERVAL doesn't, so a bad stretch can't hide in a gap that grows
# with runtime -- this is what actually catches alass being subtly wrong somewhere the content
# check's sparser windows happened to miss.
FULL_MODE_ANCHOR_INTERVAL_S = 60.0


def collect_samples_full(video_path: Path, subs, sub_lang: Optional[str], cfg: Config, tmp_dir: Path,
                          conn, cancel_event=None) -> dict:
    """sync.whisper_mode == "full" version of collect_samples() -- ONE full-episode/movie Whisper
    transcription (generate.full_transcript_for_check, cached per video, reused across languages
    and runs) instead of a per-slot Whisper call. Returns the same shape as collect_samples(), so
    finalize_line_order/pipeline.py need no changes to consume it.

    Two independent passes, since they have different cost profiles now that the whole
    transcript is already fetched:
      - Line-order: EVERY 2-line event is tested (all_two_line_events, not just
        heuristic_candidates' cap-signal pre-filter) directly against its own window of the
        already-fetched segments -- no clustering/slot competition needed (collect_samples'
        region-slot approach exists because THAT'S a real, paid Whisper call to share; unbounded
        is free here). Testing every event used to be noise (~27% false-positive rate on real
        fixtures) until _judge_order got a bridging-segment guard: a candidate only counts if one
        single Whisper segment's own text spans both lines' vocabulary, i.e. Whisper itself heard
        it as one utterance, not two separate sentences that happen to share words in reverse
        order. With that guard, testing everything found 5x the confirmed swaps of the
        cap-signal-only approach with 0 known false positives left on the same fixtures (still
        line_issues/auto-fix only -- line_flagged stays restricted to heuristic_candidates hits,
        so a low-confidence "unconfirmed" report doesn't drown in noise from ordinary dialogue
        that was never suspected of anything).
      - Correctness score: still n = sample_count * FULL_MODE_SAMPLE_MULTIPLIER evenly-spread
        windows, since a window CAN cost an LLM translation call for a foreign-language subtitle
        (_compare_transcript_to_window) -- that's the one part of this that isn't free."""
    duration = get_duration_seconds(video_path)
    if not duration:
        return {"skipped": True, "reason": "could not read duration (ffprobe)"}
    ffprobe_lang = detect_audio_language_ffprobe(video_path)
    if cfg.require_audio_lang and ffprobe_lang and ffprobe_lang != cfg.require_audio_lang:
        reason = f"speech is '{ffprobe_lang}' (per the file's metadata), not '{cfg.require_audio_lang}' — skipped"
        return {"skipped": True, "reason": reason}

    spoken_lang, segments = generate.full_transcript_for_check(cfg, video_path, tmp_dir, conn,
                                                                 cancel_event=cancel_event)
    if not segments:
        return {"skipped": True, "reason": "full-track transcription produced no usable segments"}

    audio_lang = ffprobe_lang or spoken_lang
    if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
        return {"skipped": True, "reason": f"speech is '{audio_lang}', not '{cfg.require_audio_lang}' — skipped"}
    lang = audio_lang or cfg.require_audio_lang

    candidates = all_two_line_events(subs)
    heuristic_indices = [i for i, *_ in heuristic_candidates(subs)]
    whisper_verdicts: dict[int, Optional[bool]] = {i: None for i, *_ in candidates}
    tested_items: list[tuple[int, str, str]] = []
    for i, l1, l2, start_ms, end_ms in candidates:
        raw_start, raw_end = start_ms / 1000.0, end_ms / 1000.0
        win_lo, win_hi = max(0.0, raw_start - PAD_SECONDS), raw_end + PAD_SECONDS
        # Overlap, not "starts inside": a long segment beginning just before the window is
        # exactly the bridging segment _judge_order needs, and selecting by start threw it away.
        clip_segs = [s for s in segments if s["end"] > win_lo and s["start"] < win_hi]
        whisper_verdicts[i] = _judge_order(clip_segs, raw_start, raw_end, l1, l2)
        tested_items.append((i, l1, l2))

    window_before = cfg.window_minutes * 60
    window_after = cfg.clip_seconds + cfg.window_minutes * 60
    n = max(1, cfg.sample_count) * FULL_MODE_SAMPLE_MULTIPLIER
    regions = [(duration * i / n, duration * (i + 1) / n) for i in range(n)]
    samples: list[dict] = []

    for region_start, region_end in regions:
        clip_start = pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)
        clip_end = clip_start + cfg.clip_seconds
        window_text = subs_text_in_window(subs, clip_start, window_before, window_after)
        match_lo, match_hi = clip_start - window_before, clip_start + window_after
        clip_segs = [s for s in segments if clip_start <= s["start"] < clip_end]

        if not clip_segs:
            continue  # nothing said in this stretch -- not worth a sample
        transcript_text = " ".join(s.get("text", "") for s in clip_segs)
        anchor_info = (clip_anchor_shift(clip_segs, 0.0, subs, match_lo, match_hi)
                       if anchors_applicable(sub_lang, lang) else None)
        compare = _compare_transcript_to_window(cfg, transcript_text, window_text, sub_lang, lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": round(clip_start, 1), "error": compare["error"], "anchor": anchor_info})
        else:
            samples.append({"start": round(clip_start, 1), "anchor": anchor_info, **compare})

    if anchors_applicable(sub_lang, lang):
        covered = {round(s["start"]) for s in samples}
        t = 0.0
        while t < duration:
            if not any(abs(t - c) < FULL_MODE_ANCHOR_INTERVAL_S / 2 for c in covered):
                clip_segs = [s for s in segments if t <= s["start"] < t + cfg.clip_seconds]
                anchor_info = (clip_anchor_shift(clip_segs, 0.0, subs, t - window_before, t + window_after)
                               if clip_segs else None)
                if anchor_info:
                    samples.append({"start": round(t, 1), "anchor": anchor_info})
                    covered.add(round(t))
            t += FULL_MODE_ANCHOR_INTERVAL_S

    return {"skipped": False, "samples": samples, "audio_lang": audio_lang,
            "whisper_verdicts": whisper_verdicts, "tested_items": tested_items, "candidates": candidates,
            "heuristic_indices": heuristic_indices}


def finalize_line_order(collected: dict, cfg: Config, cancel_event=None, run_llm_confirm: bool = True) -> dict:
    """Turns a collect_samples() result — fresh, or reused from a previous run's cache (see
    pipeline.py) — into the actual verdict. Every Whisper-confirmed swap gets auto-fixed
    (line_issues) -- swapping two lines back is a safe, mechanical fix regardless of how many
    lines are affected; a real content problem (wrong episode, bad translation) is caught
    separately, by the ordinary correctness score, which is what decides whether the file is
    trustworthy at all (see pipeline.py). swap_severity is Whisper's own confirmed rate, purely
    informational (noted when auto-fixing) -- run_llm_confirm=False (line-order feature not
    turned on) just skips computing it, nothing else.

    Returns the same shape correctness_check does ({"avg_score", "samples", "flag", "audio_lang"}),
    plus:
      "swap_severity": None, or {"whisper_rate", "whisper_confirmed", "whisper_checked",
        "sample_size"} when Whisper's own confirmed rate covers a large share of the TESTED
        heuristic candidates. Always None when run_llm_confirm is False.
      "line_issues": [{"index", "l1", "l2"}] — tested candidates Whisper itself confirmed swapped.
      "line_flagged": [{"index", "l1", "l2"}] — heuristic hits that were NOT confirmed either way
        (never tested because their region already had an earlier candidate, or tested but
        inconclusive) — reported for visibility, never auto-fixed. Full mode tests every 2-line
        event for line_issues, but line_flagged stays restricted to heuristic_candidates hits (see
        collect_samples_full) -- an ordinary line that was never text-suspicious in the first
        place isn't worth surfacing just because full mode happened to test it too."""
    samples = collected["samples"]
    whisper_verdicts = collected["whisper_verdicts"]
    tested_items = collected["tested_items"]
    candidates = collected["candidates"]
    heuristic_idx = collected.get("heuristic_indices")
    heuristic_set = set(heuristic_idx) if heuristic_idx is not None else None

    avg, flag = _aggregate_correctness(samples, cfg)

    swap_severity = None
    w_confirmed, w_checked = _rate(whisper_verdicts)
    if run_llm_confirm and _meets_swap_threshold(w_confirmed, w_checked, cfg):
        swap_severity = {
            "whisper_rate": round(w_confirmed / w_checked, 3), "whisper_confirmed": w_confirmed,
            "whisper_checked": w_checked, "sample_size": len(tested_items),
        }

    line_issues, line_flagged = [], []
    by_index = {i: (l1, l2) for i, l1, l2, *_ in candidates}
    for i, verdict in whisper_verdicts.items():
        if verdict is True:
            line_issues.append({"index": i, "l1": by_index[i][0], "l2": by_index[i][1]})
        elif verdict is None and (heuristic_set is None or i in heuristic_set):
            # Tested-but-inconclusive, or never in the sample at all (outside the budget) —
            # either way, unresolved, report for visibility but never auto-fix. Restricted to
            # heuristic hits (see docstring) -- an ordinary, never-suspicious line tested only
            # because full mode tests everything has nothing to report here.
            line_flagged.append({"index": i, "l1": by_index[i][0], "l2": by_index[i][1]})

    return {"skipped": False, "avg_score": avg, "samples": samples, "flag": flag,
            "audio_lang": collected["audio_lang"], "swap_severity": swap_severity,
            "line_issues": line_issues, "line_flagged": line_flagged}


def check_subtitle(video_path: Path, subs, sub_lang: Optional[str], cfg: Config, tmp_dir: Path,
                    conn=None, cancel_event=None) -> dict:
    """collect_samples() + finalize_line_order() in one call, with the LLM confirmation always
    on — a plain end-to-end entry point for a caller that doesn't need cross-run caching.
    pipeline.py calls the two halves separately instead, so it can reuse a cached collect_samples()
    result instead of re-transcribing (see cache_key_for)."""
    collected = collect_samples(video_path, subs, sub_lang, cfg, tmp_dir, conn=conn, cancel_event=cancel_event)
    if collected.get("skipped"):
        return collected
    return finalize_line_order(collected, cfg, cancel_event=cancel_event, run_llm_confirm=True)


def apply_line_swap(subs, index: int) -> None:
    """Swaps the two lines of one event in place, preserving pysubs2's \\N line-break marker."""
    e = subs.events[index]
    l1, l2 = _split_two_lines(e.text)
    e.text = f"{l2}\\N{l1}"
