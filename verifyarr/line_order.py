"""Detects two-line subtitle entries where the two lines are in the wrong (reversed) reading
order, e.g. line 2 is actually spoken before line 1. Auto-fixing/flagging is off unless
sync.line_order_enabled is on (see Settings -> Automation's "What runs" table), but the two
cheap layers below actually run — and get cached — every time a correctness check does, whether
or not line-order is on (see pipeline.py): there's no reason not to, since layer 2 rides on the
same Whisper clips correctness is already sending. That way, turning line-order on later doesn't
mean re-transcribing anything already checked since the subtitle last changed — only layer 3
(the only one that costs its own API call) still has to run.

Three layers, cheapest first:

1. A free, local heuristic (_cap_signal): line 1 starts lowercase AND line 2 starts uppercase,
   UNLESS line 1 ends in ./!/? (then it's a complete sentence on its own — L2 starting a new
   capitalized sentence right after it is normal, not a swap signal). Validated on real data
   (Community S02/S03): most heuristic-only hits don't hold up against audio confirmation —
   this is a candidate PRE-FILTER, never a verdict on its own.

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

3. A batched LLM opinion (same tested candidates, one call), used ONLY as a second, independent
   confirmation when Whisper already thinks a large share of the TESTED candidates are genuinely
   swapped, and only when line-order is actually turned on (see finalize_line_order's
   run_llm_confirm) — deciding "is this subtitle bad enough to replace", not per-line judgments.

Per-line auto-fix (apply_line_swap) only ever happens where the heuristic AND Whisper both
agree on that SPECIFIC line — the LLM layer never feeds into per-line fixes, only into the
whole-file "this needs a new subtitle" decision."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr.correctness import (JobCancelled, _LLM_URLS, _aggregate_correctness, _compare_transcript_to_window,
                                    _post_ratelimited, _stt_model_and_fallback, _transcribe_once,
                                    detect_audio_language_ffprobe, extract_clip, get_duration_seconds)
from verifyarr.settings import Config
from verifyarr.subtitles import pick_dialogue_dense_time, subs_fingerprint, subs_text_in_window, tokenize

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

# Batch LLM call: initial and retry (on finish_reason=="length") completion token budgets. Sized
# generously above the largest sample count any reasonable settings.sample_count_* would produce.
LLM_BATCH_MAX_TOKENS = 3500
LLM_BATCH_RETRY_MAX_TOKENS = 7000


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


def heuristic_candidates(subs) -> list[tuple[int, str, str, int, int]]:
    """Every 2-line event where _cap_signal's free, local heuristic flags a possible reversed
    line order — a candidate PRE-FILTER, never a verdict on its own (see module docstring).
    (index, l1, l2, start_ms, end_ms), in subtitle order."""
    out = []
    for i, e in enumerate(subs.events):
        split = _split_two_lines(e.text)
        if split and _cap_signal(split[0], split[1]):
            out.append((i, split[0], split[1], e.start, e.end))
    return out


def cache_key_for(subs, cfg: Config) -> str:
    """Identifies a `collect_samples` result as still valid for THIS subtitle content under
    THESE settings (see pipeline.py) — a subtitle's fingerprint (content, not file mtime/size)
    plus every setting that changes which clips get picked/how they're judged. Any change to the
    subtitle or these settings naturally produces a different key, so a stale cache is never
    reused by accident."""
    return f"{subs_fingerprint(subs)}:{cfg.sample_count}:{cfg.clip_seconds}:{cfg.window_minutes}"


def _window_subtitle_text(subs, start_sec: float, end_sec: float) -> str:
    """Every subtitle line displayed within [start_sec, end_sec] — not just the 2-line
    candidates a cluster happens to contain, so the correctness comparison sees the same kind of
    "what does the subtitle claim is being said here" text correctness_check compares against."""
    lo_ms, hi_ms = start_sec * 1000, end_sec * 1000
    return "\n".join(e.plaintext for e in subs.events if lo_ms <= e.start <= hi_ms)


def _cluster_windows(candidates: list[tuple[int, str, str, int, int]]) -> list[dict]:
    """candidates: (index, l1, l2, start_ms, end_ms). Returns clusters, sorted by time, each
    {"clip_start": sec, "clip_end": sec, "items": [(index, l1, l2, raw_start_sec, raw_end_sec)]}
    — clip_start/clip_end (WITH padding) are the cluster's outer bounds, what actually gets
    extracted from the video and what clustering/merging decisions are based on. raw_start/
    raw_end are each candidate's own ACTUAL, unpadded subtitle timing — kept separately (not the
    padded window) because _judge_order needs to know exactly which Whisper segments genuinely
    belong to THIS candidate; using the padded window there previously pulled in neighboring
    segments from adjacent dialogue and broke the segment-count/sequence-match logic (found
    while debugging a real miss)."""
    windows = sorted(
        ((i, l1, l2, s / 1000.0, e / 1000.0, max(0.0, s / 1000.0 - PAD_SECONDS), e / 1000.0 + PAD_SECONDS)
         for i, l1, l2, s, e in candidates),
        key=lambda w: w[5],  # sort by padded win_start
    )
    clusters: list[dict] = []
    for i, l1, l2, raw_start, raw_end, win_start, win_end in windows:
        item = (i, l1, l2, raw_start, raw_end)
        if (clusters and win_start - clusters[-1]["clip_end"] <= MERGE_GAP_SECONDS
                and win_end - clusters[-1]["clip_start"] <= MAX_CLIP_SECONDS):
            clusters[-1]["items"].append(item)
            clusters[-1]["clip_end"] = max(clusters[-1]["clip_end"], win_end)
        else:
            clusters.append({"clip_start": win_start, "clip_end": win_end, "items": [item]})
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
    displayed = _normalize_for_sequence_match(f"{l1} {l2}")
    swapped = _normalize_for_sequence_match(f"{l2} {l1}")
    displayed_score = difflib.SequenceMatcher(None, t, displayed).ratio()
    swapped_score = difflib.SequenceMatcher(None, t, swapped).ratio()
    if swapped_score - displayed_score >= SEQUENCE_MARGIN:
        return True
    if displayed_score - swapped_score >= SEQUENCE_MARGIN:
        return False
    return None


def _judge_order(segments: list[dict], rel_start: float, rel_end: float, l1: str, l2: str) -> Optional[bool]:
    """True = audio confirms L2-then-L1 (swapped) fits better. False = audio confirms the
    displayed L1-then-L2 order fits better. None = inconclusive."""
    # Midpoint-inside-window, not "touches the window at all" — a segment that only grazes the
    # boundary must NOT count as belonging to this candidate (found in testing, S03E01 #69).
    overlapping = sorted((s for s in segments if rel_start <= (s["start"] + s["end"]) / 2 <= rel_end),
                          key=lambda s: s["start"])
    if len(overlapping) < 2:
        return _sequence_order_verdict(" ".join(s["text"] for s in overlapping), l1, l2)
    mid = (rel_start + rel_end) / 2
    first_text = " ".join(s["text"] for s in overlapping if (s["start"] + s["end"]) / 2 < mid)
    second_text = " ".join(s["text"] for s in overlapping if (s["start"] + s["end"]) / 2 >= mid)
    if not first_text.strip() or not second_text.strip():
        return None

    t_first, t_second = tokenize(first_text), tokenize(second_text)
    l1_tok, l2_tok = tokenize(l1), tokenize(l2)

    def overlap(transcript_tokens: set[str], line_tokens: set[str]) -> float:
        return (len(transcript_tokens & line_tokens) / len(line_tokens)) if line_tokens else 0.0

    displayed_score = overlap(t_first, l1_tok) + overlap(t_second, l2_tok)
    swapped_score = overlap(t_first, l2_tok) + overlap(t_second, l1_tok)
    if swapped_score - displayed_score >= SWAP_MARGIN:
        return True
    if displayed_score - swapped_score >= SWAP_MARGIN:
        return False
    return None


_LLM_BATCH_SYS_PROMPT = (
    "You are checking two-line subtitle entries for line-order errors. Each entry has L1 "
    "(displayed on top) and L2 (displayed below). Sometimes the two lines were swapped by "
    "mistake, so L2 is actually spoken/read before L1. Read each entry and decide whether "
    "L1 then L2 reads as natural, grammatically correct English, or whether L2 then L1 reads "
    "better. Output ONLY a JSON array of the index numbers where the lines are SWAPPED "
    "(i.e. L2 should come before L1). No prose, no markdown, just the JSON array, e.g. [3,9]. "
    "If none are swapped, output []."
)


def _llm_batch_call(cfg: Config, prompt: str, max_tokens: int, cancel_event=None):
    """One call. Returns (swapped_indices: set|None, needs_retry: bool). needs_retry is True only
    for finish_reason=="length" (ran out of budget before finishing — worth one retry with a
    bigger budget); every other failure mode (bad HTTP status, empty content, unparseable JSON)
    returns (None, False) — no retry, that candidate batch just stays inconclusive."""
    provider = cfg.stt_provider
    model = cfg.openrouter_llm_model if provider == "openrouter" else cfg.groq_llm_model
    headers = {"Authorization": f"Bearer {cfg.active_stt_api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "temperature": 0, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"
    try:
        resp = _post_ratelimited(_LLM_URLS[provider], headers, 60, json=payload, cancel_event=cancel_event)
    except JobCancelled:
        raise
    except Exception as e:
        log.warning("Line-order LLM batch check failed: %s", e)
        return None, False
    if resp.status_code != 200:
        log.warning("Line-order LLM batch check failed (%s): %s", resp.status_code, resp.text[:200])
        return None, False

    choice = resp.json()["choices"][0]
    finish_reason = choice.get("finish_reason")
    content = (choice.get("message", {}).get("content") or "").strip()
    if finish_reason == "length":
        # Confirmed as a real failure mode in testing: a reasoning model can burn its ENTIRE
        # token budget on invisible "thinking" and never write the actual answer, silently
        # returning empty content. finish_reason is the reliable signal for this — NOT an empty
        # string, which can also legitimately mean "no swaps found" in other failure paths.
        log.info("Line-order LLM batch check ran out of its token budget before finishing "
                 "(finish_reason=length) — worth a retry with more room.")
        return None, True
    if finish_reason != "stop" or not content:
        log.warning("Line-order LLM batch check: unusable response (finish_reason=%s, content=%r)",
                    finish_reason, content[:100])
        return None, False
    match = re.search(r"\[[\d,\s]*\]", content)
    if not match:
        log.warning("Line-order LLM batch check returned unparseable output: %r", content[:200])
        return None, False
    try:
        return {int(i) for i in json.loads(match.group(0))}, False
    except Exception as e:
        log.warning("Line-order LLM batch check returned unparseable output: %s", e)
        return None, False


def _llm_confirm_swaps(candidates: list[tuple[int, str, str]], cfg: Config,
                        cancel_event=None) -> dict[int, Optional[bool]]:
    """Batched LLM opinion on the SAME candidates Whisper already looked at (check_subtitle's
    second, independent signal) — one call, not one per candidate (validated: ~44% of the tokens
    of one-at-a-time for the same candidates). Returns {index: True/False/None}
    — None (inconclusive) whenever the call didn't produce a trustworthy answer. An inconclusive
    result must never be silently treated as "not swapped" by the caller (see _rate below)."""
    if not candidates:
        return {}
    body = "\n".join(f'{i}: L1="{l1}" | L2="{l2}"' for i, l1, l2 in candidates)
    prompt = _LLM_BATCH_SYS_PROMPT + "\n\n" + body

    swapped, needs_retry = _llm_batch_call(cfg, prompt, LLM_BATCH_MAX_TOKENS, cancel_event=cancel_event)
    if needs_retry:
        swapped, _ = _llm_batch_call(cfg, prompt, LLM_BATCH_RETRY_MAX_TOKENS, cancel_event=cancel_event)
        # A second finish_reason=="length" isn't retried again — a real second chance was given,
        # still not enough, give up cleanly rather than escalate the budget indefinitely.

    if swapped is None:
        return {i: None for i, _, _ in candidates}
    return {i: (i in swapped) for i, _, _ in candidates}


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


def _extract_and_transcribe(video_path: Path, start_sec: float, duration_sec: float, cfg: Config,
                             api_key: str, model: str, fallback: Optional[str], audio_lang: Optional[str],
                             tmp_dir: Path, cancel_event=None) -> Optional[dict]:
    """Extract one clip and transcribe it (verbose_json, with a model-fallback retry on failure —
    same policy as correctness.transcribe()/detect_language_and_transcribe(), just always
    verbose_json since check_subtitle needs segment timing for the heuristic-anchored clips and
    reuses the same call shape for the filler ones for simplicity). Returns the parsed response,
    or None if extraction or every transcription attempt failed."""
    clip_path = tmp_dir / f"clip_{int(start_sec)}.wav"
    try:
        if not extract_clip(video_path, start_sec, round(duration_sec, 1), clip_path):
            log.warning("check_subtitle: could not extract clip at %.1fs for %s", start_sec, video_path.name)
            return None
        try:
            return _transcribe_once(cfg.stt_provider, clip_path, api_key, model, language=audio_lang,
                                     response_format="verbose_json", cancel_event=cancel_event,
                                     fail_fast_on_429=bool(fallback))
        except JobCancelled:
            raise
        except Exception as e:
            if not fallback or fallback == model:
                log.warning("check_subtitle: transcription failed for %s: %s", video_path.name, e)
                return None
            try:
                return _transcribe_once(cfg.stt_provider, clip_path, api_key, fallback, language=audio_lang,
                                         response_format="verbose_json", cancel_event=cancel_event)
            except Exception as e2:
                log.warning("check_subtitle: transcription failed for %s: %s", video_path.name, e2)
                return None
    finally:
        clip_path.unlink(missing_ok=True)


def collect_samples(video_path: Path, subs, sub_lang: Optional[str], cfg: Config, tmp_dir: Path,
                     cancel_event=None) -> dict:
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

    Known limitation: unlike correctness_check, this does not share transcripts across a video's
    multiple subtitle languages (heuristic candidates, and therefore clip placement, differ per
    subtitle file) — each language pays for its own sampling pass. Still fewer total Whisper
    calls than the old design (a separate, uncached line-order pass ran on top of
    correctness_check's own cached pass for every language)."""
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

    # One slot per region: ("heuristic", cluster) if a candidate cluster starts in it, else
    # ("filler", start_sec) at that region's most dialogue-dense point. A cluster only ever
    # starts in exactly one region, so no cluster can be picked twice here.
    slots: list[tuple[str, object]] = []
    for region_start, region_end in regions:
        in_region = next((c for c in clusters if region_start <= c["clip_start"] < region_end), None)
        if in_region is not None:
            slots.append(("heuristic", in_region))
        else:
            slots.append(("filler", pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)))

    api_key = cfg.active_stt_api_key
    model, fallback = _stt_model_and_fallback(cfg)

    samples: list[dict] = []
    whisper_verdicts: dict[int, Optional[bool]] = {i: None for i, *_ in candidates}
    tested_items: list[tuple[int, str, str]] = []  # (index, l1, l2) actually sent to Whisper

    def _run_clip(start_sec: float, clip_duration: float) -> Optional[dict]:
        nonlocal audio_lang, lang
        result = _extract_and_transcribe(video_path, start_sec, clip_duration, cfg, api_key, model,
                                          fallback, audio_lang, tmp_dir, cancel_event=cancel_event)
        if result is None:
            return None
        if audio_lang is None:
            audio_lang = result.get("language")
            lang = audio_lang or cfg.require_audio_lang
        return result

    for kind, slot in slots:
        if kind == "heuristic":
            cluster = slot
            start, clip_duration = cluster["clip_start"], cluster["clip_end"] - cluster["clip_start"]
        else:
            start, clip_duration = slot, cfg.clip_seconds

        result = _run_clip(start, clip_duration)
        if result is None:
            samples.append({"start": round(start, 1), "error": "audio extraction/transcription failed"})
            continue
        if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
            return {"skipped": True, "reason": f"speech is '{audio_lang}', not '{cfg.require_audio_lang}' — skipped"}

        if kind == "heuristic":
            segments = result.get("segments") or []
            transcript_text = " ".join(s.get("text", "") for s in segments)
            window_text = _window_subtitle_text(subs, cluster["clip_start"], cluster["clip_end"])
            for i, l1, l2, raw_start, raw_end in cluster["items"]:
                rel_start, rel_end = raw_start - cluster["clip_start"], raw_end - cluster["clip_start"]
                whisper_verdicts[i] = _judge_order(segments, rel_start, rel_end, l1, l2)
                tested_items.append((i, l1, l2))
        else:
            transcript_text = " ".join(s.get("text", "") for s in (result.get("segments") or []))
            window_text = subs_text_in_window(subs, start, cfg.window_minutes * 60,
                                                cfg.clip_seconds + cfg.window_minutes * 60)

        compare = _compare_transcript_to_window(cfg, transcript_text, window_text, sub_lang, lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": round(start, 1), "error": compare["error"]})
        else:
            samples.append({"start": round(start, 1), **compare})

    return {"skipped": False, "samples": samples, "audio_lang": audio_lang,
            "whisper_verdicts": whisper_verdicts, "tested_items": tested_items, "candidates": candidates}


def finalize_line_order(collected: dict, cfg: Config, cancel_event=None, run_llm_confirm: bool = True) -> dict:
    """Turns a collect_samples() result — fresh, or reused from a previous run's cache (see
    pipeline.py) — into the actual verdict. The Whisper-derived candidate verdicts are already
    known at this point (free either way); only the widespread-swap LLM confirmation costs an API
    call, so run_llm_confirm=False (used whenever the line-order feature isn't actually turned on
    — see pipeline.py) skips just that part, never the (cheap) per-line reporting below it.

    Returns the same shape correctness_check does ({"avg_score", "samples", "flag", "audio_lang"}),
    plus:
      "swap_severity": None, or {"whisper_rate", "whisper_confirmed", "whisper_checked",
        "llm_rate", "llm_confirmed", "llm_checked", "sample_size"} when both Whisper AND an
        independent LLM pass confirm a large share of the TESTED heuristic candidates are
        genuinely swapped — pipeline.py treats this like a correctness SUSPECT (blacklist
        candidate), not a per-line fix. Always None when run_llm_confirm is False.
      "line_issues": [{"index", "l1", "l2"}] — tested candidates Whisper itself confirmed
        swapped; only meaningful when swap_severity is None (no point fixing individual lines in
        a file that's about to be replaced).
      "line_flagged": [{"index", "l1", "l2"}] — heuristic hits that were NOT confirmed either way
        (never tested because their region already had an earlier candidate, or tested but
        inconclusive) — reported for visibility, never auto-fixed."""
    samples = collected["samples"]
    whisper_verdicts = collected["whisper_verdicts"]
    tested_items = collected["tested_items"]
    candidates = collected["candidates"]

    avg, flag = _aggregate_correctness(samples, cfg)

    swap_severity = None
    w_confirmed, w_checked = _rate(whisper_verdicts)
    if run_llm_confirm and _meets_swap_threshold(w_confirmed, w_checked, cfg):
        llm_verdicts = _llm_confirm_swaps(tested_items, cfg, cancel_event=cancel_event)
        l_confirmed, l_checked = _rate(llm_verdicts)
        if _meets_swap_threshold(l_confirmed, l_checked, cfg):
            swap_severity = {
                "whisper_rate": round(w_confirmed / w_checked, 3), "whisper_confirmed": w_confirmed,
                "whisper_checked": w_checked,
                "llm_rate": round(l_confirmed / l_checked, 3) if l_checked else None,
                "llm_confirmed": l_confirmed, "llm_checked": l_checked, "sample_size": len(tested_items),
            }

    line_issues, line_flagged = [], []
    if swap_severity is None:
        by_index = {i: (l1, l2) for i, l1, l2, *_ in candidates}
        for i, verdict in whisper_verdicts.items():
            if verdict is True:
                line_issues.append({"index": i, "l1": by_index[i][0], "l2": by_index[i][1]})
            elif verdict is None:
                # Tested-but-inconclusive, or never in the sample at all (outside the budget) —
                # either way, unresolved, report for visibility but never auto-fix.
                line_flagged.append({"index": i, "l1": by_index[i][0], "l2": by_index[i][1]})

    return {"skipped": False, "avg_score": avg, "samples": samples, "flag": flag,
            "audio_lang": collected["audio_lang"], "swap_severity": swap_severity,
            "line_issues": line_issues, "line_flagged": line_flagged}


def check_subtitle(video_path: Path, subs, sub_lang: Optional[str], cfg: Config, tmp_dir: Path,
                    cancel_event=None) -> dict:
    """collect_samples() + finalize_line_order() in one call, with the LLM confirmation always
    on — a plain end-to-end entry point for a caller that doesn't need cross-run caching.
    pipeline.py calls the two halves separately instead, so it can reuse a cached collect_samples()
    result instead of re-transcribing (see cache_key_for)."""
    collected = collect_samples(video_path, subs, sub_lang, cfg, tmp_dir, cancel_event=cancel_event)
    if collected.get("skipped"):
        return collected
    return finalize_line_order(collected, cfg, cancel_event=cancel_event, run_llm_confirm=True)


def apply_line_swap(subs, index: int) -> None:
    """Swaps the two lines of one event in place, preserving pysubs2's \\N line-break marker."""
    e = subs.events[index]
    l1, l2 = _split_two_lines(e.text)
    e.text = f"{l2}\\N{l1}"
