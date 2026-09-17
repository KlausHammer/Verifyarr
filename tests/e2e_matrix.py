"""Offset/sync matrix test: corruption scenarios x Whisper-model axis.

Extends tests/e2e_offset.py (which runs piecewise/drift/gap against the
large-v3-turbo fixture only) in two directions: more corruption types, and the
audio evidence coming from a chosen model instead of only the turbo fixture.

Episodes (10: 9 user-confirmed + timing-trusted, plus C_S03E04):
  C_S03E03 C_S03E08 C_S03E10 SH_S01E01 SH_S01E02 SH_S01E03
  SH_S01E04 SH_S01E05 SH_S01E06 (clean facit set)
  C_S03E04 (documented real drift case: user-confirmed correct content
  but -0.048 s/min genuine drift -- see DRIFT_CASE_SLUGS).

Scenarios (6 + 1 opt-in extra):
  clean      no corruption; control group measuring false positives
             ("already in sync", no flag, no rewritten timings).
  uniform    global +45s shift (the old e2e_after.py scenario).
  drift      progressive 2% stretch (25<->23.976fps style).
  piecewise  6 blocks, each seeded random +/-5..15s.
  swap       in-cue L1/L2 line reversal -- see corrupt_swap docstring.
  gap        5-minute middle chunk deleted (cut version).
  drift_swap COMBINED drift (2%) + swap. Opt-in extra, kept out of the
             default set: realistic (a frame-rate-converted file can carry
             both), and worth knowing whether the two fixes interfere.

Model axis: "turbo" is tests/fixtures/whisper_full/<slug>.json
(large-v3-turbo, the baseline); the rest is
whisper_gpu_staging/sweep/<config>/<slug>.json (whisper.cpp JSON, same shape
as the main dataset -- segments under "transcription" with ms "offsets",
see DATAFORMAT.md's unit table). Normalized to fixture shape
({start,end (s),text}) on load. Missing (model, episode) combos are SKIPPED
with a marked row, never an error (known: medium.en-q5_0 / SH_S01E03 --
upstream whisper.cpp DTW crash, permanent).

Full matrix: 15 models x 10 episodes x 6 scenarios x 2 modes x 2 audio-confirm
= 3600 runs. Resumable: completed (model, episode, scenario, mode, audio) rows
are read back from the output file and not re-run (use --redo to force);
skipped rows are always retried.

Mode axis: "full" isolates model quality (whole transcript, no sampling noise);
"sampled" runs the real production defaults (5 clips, escalation to full) with
clips sliced out of the same full transcript, exactly like min_coverage.py's
sampled_eval -- same positions collect_samples would pick (dialogue-dense per
region), same start-inside selection rule. No new Whisper calls; the sampled
vs full delta therefore measures coverage loss only, not short-clip
transcription differences. Audio-confirm axis: on = Whisper-confirmed swaps
are auto-fixed; off = heuristic candidates are only flagged.

Output (never touches e2e_offset*.jsonl):
  <out>{SUFFIX}.jsonl          one row per (model, episode, scenario, mode, audio)
  <out>_summary{SUFFIX}.json   aggregates per scenario/model/mode plus
                               cross-model index agreement and swap detail
Run: .venv/bin/python tests/e2e_matrix.py [--only C_S02E02,...]
       [--models turbo,small.en-q5_1] [--scenarios clean,uniform,...]
       [--mode full|sampled] [--audio-confirm on|off]
       [--out e2e_matrix] [--shard 0] [--redo]
Parallel: tests/e2e_matrix_parallel.py (one process per episode, merged after).
"""
from __future__ import annotations
import contextlib
import copy
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

import os as _os
# Code under test: this repo checkout by default; override with
# VERIFYARR_UNDER_TEST=/path/to/checkout (never /tmp/vwork -- off limits).
VWORK = _os.environ.get("VERIFYARR_UNDER_TEST", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, VWORK)

from verifyarr import db, generate, line_order, pipeline
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs

FIX = Path("/home/hammer/Auto sync sub/verifyarr/tests/fixtures/whisper_full")
SWEEP = Path("/mnt/c/Users/knham/Desktop/undertekst auto/whisper_gpu_staging/sweep")
DIRS = {
    "C_S02": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 2"),
    "C_S03": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 3"),
    "SH_S01": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Slow Horse/Season 1"),
}
WAV_DIR = Path("/mnt/c/Users/knham/Desktop/undertekst auto/whisper_gpu_staging/wav")
TURBO = "turbo"
SWEEP_MODELS = ["base.en-cpu", "base.en-greedy-cpu", "base.en-q5_1-cpu",
                "medium.en", "medium.en-greedy", "medium.en-q5_0",
                "small.en", "small.en-greedy", "small.en-q5_1",
                "tiny.en-cpu", "tiny.en-greedy-cpu", "tiny.en-q5_1-cpu",
                "turbo-q5_0", "turbo-q8_0"]
ALL_MODELS = [TURBO] + SWEEP_MODELS
SLUGS = ["C_S03E03", "C_S03E08", "C_S03E10",
         "SH_S01E01", "SH_S01E02", "SH_S01E03",
         "SH_S01E04", "SH_S01E05", "SH_S01E06",
         "C_S03E04"]
# Documented real drift case: user-confirmed correct content, but -0.048 s/min
# genuine drift (verifyarr_handoff/afsnit_tillid.json). Scoring recovery
# against its own timings would punish the pipeline for syncing to the audio,
# so drift-case rows carry drift_case=True and skip "recovered" -- they measure
# whether the drift is DETECTED (flag/sync), not recreated.
DRIFT_CASE_SLUGS = {"C_S03E04"}


def scores_recovery(slug, scenario):
    """Timing recovery is meaningless on drift-case episodes (own timings off)."""
    return (scenario in TIMING_SCENARIOS or scenario == "clean") \
        and slug not in DRIFT_CASE_SLUGS
TIMING_SCENARIOS = {"uniform", "drift", "piecewise", "gap", "drift_swap"}
DEFAULT_SCENARIOS = ["clean", "uniform", "drift", "piecewise", "swap", "gap"]
MODES = ["full", "sampled"]
AUDIOS = ["on", "off"]

OUT_DIR = Path(__file__).parent


def media_dir(slug):
    for p, d in DIRS.items():
        if slug.startswith(p):
            return d
    raise KeyError(slug)


def fixture(slug):
    return json.loads((FIX / f"{slug}.json").read_text(encoding="utf-8"))


def sweep_segments(doc):
    """whisper.cpp sweep JSON -> fixture-shaped [{start,end (s),text}].

    Sweep entries live under "transcription" with ms "offsets" (DATAFORMAT.md:
    offsets.from/.to are ms; text "timestamps" are display strings). Fixtures
    use seconds floats. Empty/degenerate entries are dropped.
    """
    segs = []
    for t in doc.get("transcription") or []:
        off = t.get("offsets") or {}
        try:
            start, end = float(off["from"]) / 1000.0, float(off["to"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        text = (t.get("text") or "").strip()
        if not text or end <= start:
            continue
        segs.append({"start": start, "end": end, "text": text})
    return segs


def sweep_language(doc):
    return (doc.get("result") or {}).get("language") \
        or (doc.get("params") or {}).get("language") or "en"


def audio_evidence(model, slug, fx):
    """(language, segments) for this (model, episode); None if data missing."""
    if model == TURBO:
        return fx["language"], fx["segments"]
    path = SWEEP / model / f"{slug}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    return sweep_language(doc), sweep_segments(doc)


def subs_for(slug, fx):
    return load_subs(media_dir(slug) / fx["subtitle_name"])


def audio_cache_for(slug, video):
    """Pre-seeded alass audio cache for one episode shard.

    The staging WAV is the same 16kHz mono PCM ffmpeg would extract, so
    resolve_alass_reference reuses it with no ffmpeg call. Missing WAV
    falls back to an empty cache (ffmpeg extraction, as before)."""
    wav = WAV_DIR / f"{slug}.wav"
    if wav.exists():
        return {video: wav}
    return {}


@contextlib.contextmanager
def patch_whisper_full(lang, segments):
    real = generate.full_transcript_for_check

    def fake(cfg, video_path, tmp_dir, conn, cancel_event=None):
        return lang, segments

    generate.full_transcript_for_check = fake
    try:
        yield
    finally:
        generate.full_transcript_for_check = real


def slice_clip_segments(segments, start_sec, duration_sec):
    """Full-transcript segments overlapping a sampled clip, rebased to clip time.

    Same selection rule as min_coverage.sampled_eval (segment START inside the
    clip): collect_samples scores the clip against Whisper's own sentence
    timing, so a segment that merely grazes the edge must not dominate it.
    Rebasing reconstructs absolute time via clip_start + seg.start exactly as
    real clip-relative Whisper output does (see _match_segments_to_lines).
    """
    out = []
    for s in segments:
        try:
            st, en = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start_sec <= st < start_sec + duration_sec and en > st:
            out.append({"start": st - start_sec, "end": en - start_sec,
                        "text": s.get("text", "")})
    return out


@contextlib.contextmanager
def patch_sampled_transcription(lang, segments):
    """Serves sampled-mode clips from the full transcript instead of Whisper.

    collect_samples spends one real STT call per clip; the sweep only has full
    transcripts, so each clip is sliced out of them (see slice_clip_segments).
    Clip POSITIONS still come from the real code (dialogue-dense per region),
    so coverage loss from sampling is measured honestly.
    """
    real = line_order._extract_and_transcribe

    def fake(video_path, start_sec, duration_sec, cfg, audio_lang, tmp_dir,
             cancel_event=None):
        clip = slice_clip_segments(segments, start_sec, duration_sec)
        if not clip:
            return None
        return {"text": " ".join(s["text"] for s in clip),
                "language": lang, "segments": clip}

    line_order._extract_and_transcribe = fake
    try:
        yield
    finally:
        line_order._extract_and_transcribe = real


def cfg_for(conn, mode="full", audio="on", **over) -> Config:
    cfg = Config.from_db(conn)
    vals = dict(
        groq_api_key="shim-no-real-key-needed", stt_provider="groq",
        backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, line_order_enabled=True,
        line_order_audio_confirm=(audio == "on"), whisper_mode=mode,
        use_local_whisper=False,
        # Sampled runs the shipped production defaults, pinned explicitly so a
        # stale shard DB can never silently change what "sampled" means.
        sample_count=5, clip_seconds=60, window_minutes=0.5,
        overlap_threshold=0.25, escalate_sampled_to_full=True,
    )
    vals.update(over)
    for k, v in vals.items():
        object.__setattr__(cfg, k, v)
    return cfg


def run_one(work, video, subs, lang, segments, cfg, conn, tag, mode="full",
            audio_cache=None):
    from verifyarr import db as _db
    if mode == "full":
        # Sampled keeps the cache empty like a production-fresh file, so VAD
        # placement falls back to dialogue density; the escalation path still
        # works through the patched full_transcript_for_check below.
        _db.save_full_transcript_cache(conn, video, lang, segments,
                                       stt_provider=cfg.stt_provider, stt_model=cfg.groq_model)
    tmp = work / f"{tag}.srt"
    subs.save(str(tmp))
    captured = {}
    real_finalize = pipeline.finalize_line_order
    real_apply = pipeline._apply_line_order
    real_screen = pipeline._screen_says_needs_full

    def spy_finalize(collected, cfg_, cancel_event=None, run_llm_confirm=True):
        res = real_finalize(collected, cfg_, cancel_event=cancel_event,
                            run_llm_confirm=run_llm_confirm)
        captured["flagged"] = sorted(i["index"] for i in res.get("line_flagged") or [])
        heur = collected.get("heuristic_indices")
        if heur is None:
            heur = [i for i, *_ in collected.get("candidates", [])]
        captured["heuristic"] = sorted(heur)
        return res

    def spy_apply(row_, result, swap_severity, current_subs, subtitle_path, cfg_, media_root):
        # Ground truth of what was WRITTEN (finalize alone also runs when
        # audio-confirm is off, where nothing is ever applied).
        captured["applied"] = sorted(i["index"] for i in result.get("line_issues") or [])
        return real_apply(row_, result, swap_severity, current_subs,
                          subtitle_path, cfg_, media_root)

    def spy_screen(collected, cfg_):
        hit = real_screen(collected, cfg_)
        captured["escalated"] = captured.get("escalated", False) or bool(hit)
        return hit

    pipeline.finalize_line_order = spy_finalize
    pipeline._apply_line_order = spy_apply
    pipeline._screen_says_needs_full = spy_screen
    # Shared per-shard cache: one episode = one audio track = one entry.
    # Never a fresh {} here -- that disables reuse and re-extracts per row.
    if audio_cache is None:
        audio_cache = {}
    try:
        with patch_whisper_full(lang, segments):
            if mode == "sampled":
                with patch_sampled_transcription(lang, segments):
                    row, cur = pipeline.sync_pair(video, tmp, "en", cfg, audio_cache, work)
                    row = pipeline.correctness_and_finish(video, tmp, "en", cfg, conn, row, cur)
            else:
                row, cur = pipeline.sync_pair(video, tmp, "en", cfg, audio_cache, work)
                row = pipeline.correctness_and_finish(video, tmp, "en", cfg, conn, row, cur)
    finally:
        pipeline.finalize_line_order = real_finalize
        pipeline._apply_line_order = real_apply
        pipeline._screen_says_needs_full = real_screen
    # update_state ignores unknown keys, so these ride along harmlessly.
    audio_on = cfg.line_order_enabled and cfg.line_order_audio_confirm
    row["lo_fixed_indices"] = captured.get("applied", [])
    # Flagged means "surfaced for review": with audio-confirm off that is the
    # whole heuristic set (never narrowed by Whisper), otherwise the
    # inconclusive remainder finalize reported.
    row["lo_flagged_indices"] = (captured.get("flagged", []) if audio_on
                                 else captured.get("heuristic", []))
    row["escalated"] = captured.get("escalated", False)
    from verifyarr.subtitles import load_subs as _load
    return row, _load(tmp)


# --- corruptions (all deterministic; RNG seeded per slug+scenario in main) ---
def corrupt_clean(subs, rng):
    return copy.deepcopy(subs), None, {}


def corrupt_uniform(subs, rng, shift_s=45.0):
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(e.start + shift_s * 1000)
        e.end = int(e.end + shift_s * 1000)
    return out, None, {"shift_s": shift_s}


def corrupt_piecewise(subs, rng, n_blocks=6, lo=5.0, hi=15.0):
    """Each contiguous time block shifted by its own random +/-lo..hi seconds."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    shifts = []
    for b in range(n_blocks):
        mag = rng.uniform(lo, hi) * rng.choice((-1, 1))
        shifts.append(mag)
        b0, b1 = dur * b / n_blocks, dur * (b + 1) / n_blocks
        for e in out.events:
            if b0 <= e.start / 1000.0 < b1:
                e.start = max(0, int(e.start + mag * 1000))
                e.end = max(e.start + 200, int(e.end + mag * 1000))
    return out, None, {"block_shifts_s": [round(s, 2) for s in shifts]}


def corrupt_drift(subs, rng, rate=0.02):
    """Progressive stretch t -> t*(1+rate): framerate-conversion style drift."""
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(e.start * (1 + rate))
        e.end = int(e.end * (1 + rate))
    return out, None, {"rate": rate}


def corrupt_gap(subs, rng, gap_seconds=300.0):
    """Delete a contiguous middle chunk (cut version). Returns kept indices."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    g0, g1 = dur * 0.4, dur * 0.4 + gap_seconds
    kept = [i for i, e in enumerate(out.events)
            if not (g0 * 1000 <= e.start and e.end <= g1 * 1000)]
    removed = [i for i in range(len(out.events)) if i not in set(kept)]
    out.events = [out.events[i] for i in kept]
    return out, kept, {"gap_start_s": round(g0, 1), "gap_end_s": round(g1, 1),
                       "removed_lines": len(removed)}


def corrupt_swap(subs, rng, n=6):
    """Reverse L1/L2 inside up to n two-line cues.

    Chosen failure mode: in-cue line reversal is exactly what
    verifyarr/line_order.py detects and auto-fixes (free cap-signal prefilter
    + Whisper audio confirmation in _judge_order, mechanical fix in
    apply_line_swap). Whole-file cue reordering is NOT used instead: it would
    defeat content matching everywhere at once (a wrong-episode verdict, not a
    line-order fix), which is a different feature's job.

    Targets mirror tests/test_sync_verification.py::LineOrderTests: only cues
    whose SWAPPED form trips _cap_signal (so each injected swap is actually
    testable -- swapping already-lowercase/uppercase pairs would be invisible
    by construction, not a detection failure), excluding cues the heuristic
    already flags in the original. First n in file order: deterministic
    without needing rng.
    """
    from verifyarr.line_order import _split_two_lines, _cap_signal, heuristic_candidates
    out = copy.deepcopy(subs)
    already = {c[0] for c in heuristic_candidates(out)}
    targets = []
    for i, e in enumerate(out.events):
        parts = _split_two_lines(e.text)
        if parts and _cap_signal(parts[1], parts[0]) and i not in already:
            targets.append(i)
            if len(targets) >= n:
                break
    for i in targets:
        l1, l2 = _split_two_lines(out.events[i].text)
        out.events[i].text = f"{l2}\\N{l1}"
    return out, None, {"swapped": targets}


def corrupt_drift_swap(subs, rng, rate=0.02, n=6):
    """Combined drift + in-cue swap: a frame-rate-converted file can carry both
    at once (timing skewed AND lines misordered), so the two fixes must not
    fight each other. Kept out of the default set; run explicitly."""
    drifted, _, d_detail = corrupt_drift(subs, rng, rate=rate)
    out, kept, s_detail = corrupt_swap(drifted, rng, n=n)
    return out, kept, {"rate": rate, "swapped": s_detail["swapped"]}


def timing_errors(ref_events, after_events, kept=None):
    """Per-line |start_after - start_ref| in seconds over matched indices."""
    idx = kept if kept is not None else range(len(ref_events))
    errs = []
    for j, i in enumerate(idx):
        if j >= len(after_events):
            break
        errs.append(abs(after_events[j].start - ref_events[i].start) / 1000.0)
    return errs


def summarize(errs):
    if not errs:
        return {"n": 0}
    s = sorted(errs)
    return {
        "n": len(errs),
        "p50": round(statistics.median(s), 3),
        "p90": round(s[min(len(s) - 1, int(0.9 * len(s)))], 3),
        "frac_le_0_5s": round(sum(1 for e in s if e <= 0.5) / len(s), 3),
        "frac_le_1_0s": round(sum(1 for e in s if e <= 1.0) / len(s), 3),
        "frac_le_2_0s": round(sum(1 for e in s if e <= 2.0) / len(s), 3),
    }


def swap_recovery(ref_events, after_events, swapped):
    if not swapped:
        return {"n_swapped": 0}
    if len(after_events) != len(ref_events):
        return {"n_swapped": len(swapped), "n_restored": 0,
                "note": "event count changed, index compare unsafe"}
    restored = sum(1 for i in swapped if after_events[i].text == ref_events[i].text)
    return {"n_swapped": len(swapped), "n_restored": restored,
            "frac_restored": round(restored / len(swapped), 3)}


def swap_index_detail(swapped, fixed_indices):
    """(a) injected indices restored, (b) non-injected indices fixed.

    Text comparison (swap_recovery) says whether the FILE reads right;
    this says whether the FIXER touched the right lines -- a fixer that
    restores the injected swaps while also rewriting a dozen innocent cues
    looks perfect on (a) and is still a false-positive problem, hence (b).
    """
    injected = set(swapped or [])
    fixed = set(fixed_indices or [])
    hit = sorted(injected & fixed)
    extra = sorted(fixed - injected)
    return {"n_injected": len(injected), "n_injected_fixed": len(hit),
            "frac_injected_fixed": round(len(hit) / len(injected), 3) if injected else None,
            "n_noninjected_fixed": len(extra), "noninjected_fixed": extra}


def agreement(sets):
    """Cross-model agreement over fixed/flagged index sets: unanimous (every
    model touched it, likely genuine) vs singleton (exactly one model did,
    likely a model artefact). Jaccard = |intersection| / |union|."""
    sets = [set(s) for s in sets]
    union = set().union(*sets) if sets else set()
    inter = set(sets[0]).intersection(*sets[1:]) if sets else set()
    counts = {}
    for s in sets:
        for i in s:
            counts[i] = counts.get(i, 0) + 1
    n = len(sets)
    singletons = sorted(i for i, c in counts.items() if c == 1)
    return {"n_models": n, "n_union": len(union), "n_unanimous": len(inter),
            "unanimous": sorted(inter),
            "jaccard": round(len(inter) / len(union), 3) if union else None,
            "n_singleton": len(singletons), "singleton": singletons}


def row_key(r):
    """Resume key; rows written before the mode/audio axes default to the old
    fixed setup (full + audio confirm on)."""
    return (r.get("model"), r.get("slug"), r.get("scenario"),
            r.get("mode", "full"), r.get("audio_confirm", "on"))


def dedupe_rows(rows):
    """One row per resume key: a completed (ok/error) row supersedes the stale
    skipped row kept from an earlier run before that combo's sweep data had
    landed. Skipped rows are always retried, so both can coexist in one file.
    Last completed row wins (a --redo re-run is newer than what it replaced)."""
    best = {}
    order = []
    for r in rows:
        k = row_key(r)
        if k not in best:
            order.append(k)
        if r.get("status", "ok") in ("ok", "error") or k not in best:
            best[k] = r
    return [best[k] for k in order]


SCENARIOS = {"clean": corrupt_clean, "uniform": corrupt_uniform, "drift": corrupt_drift,
             "piecewise": corrupt_piecewise, "swap": corrupt_swap, "gap": corrupt_gap,
             "drift_swap": corrupt_drift_swap}


def rows_done(path):
    """(model, slug, scenario, mode, audio) keys completed (ok/error, not skipped)."""
    rows, done = load_prior_rows(path, None, None, None, None, None, False)
    return done


def load_prior_rows(path, models, slugs, scen, modes, audios, redo):
    """Prior output rows to keep plus completed keys. Without redo, everything
    is kept and completed keys are skipped by the caller; with redo, only rows
    outside this invocation's selection are kept. Skipped rows are never
    completed (sweep data may have arrived since)."""
    rows, done = [], set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            owned = (models is None or (r.get("model") in models and r.get("slug") in slugs
                                        and r.get("scenario") in scen
                                        and r.get("mode", "full") in (modes or MODES)
                                        and r.get("audio_confirm", "on") in (audios or AUDIOS)))
            if owned and redo:
                continue
            rows.append(r)
            if r.get("status", "ok") in ("ok", "error"):
                done.add(row_key(r))
    return rows, done


def _parse_list_arg(flag, argv):
    return argv[argv.index(flag) + 1].split(",") if flag in argv else None


def _skip_row(results, done, model, slug, name, mode, audio, reason):
    key = (model, slug, name, mode, audio)
    if key in done:
        return
    results.append(dict(status="skipped", model=model, slug=slug, scenario=name,
                        mode=mode, audio_confirm=audio, reason=reason))
    done.add(key)
    print(f"{slug}.{model}.{name}.{mode}.{audio}: SKIPPED ({reason})", flush=True)


def main(argv=None):
    argv = argv or sys.argv
    only = _parse_list_arg("--only", argv)
    models = _parse_list_arg("--models", argv) or ALL_MODELS
    scen = _parse_list_arg("--scenarios", argv) or list(DEFAULT_SCENARIOS)
    modes = _parse_list_arg("--mode", argv) or list(MODES)
    audios = _parse_list_arg("--audio-confirm", argv) or list(AUDIOS)
    out_stem = argv[argv.index("--out") + 1] if "--out" in argv else "e2e_matrix"
    shard = argv[argv.index("--shard") + 1] if "--shard" in argv else ""
    redo = "--redo" in argv
    suffix = f"_{shard}" if shard else ""
    work = OUT_DIR / f"e2e_work_matrix{suffix}"
    work.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{out_stem}{suffix}.jsonl"
    bad_models = [m for m in models if m not in ALL_MODELS]
    if bad_models:
        raise SystemExit(f"unknown models: {bad_models} (choose from {ALL_MODELS})")
    bad_scen = [s for s in scen if s not in SCENARIOS]
    if bad_scen:
        raise SystemExit(f"unknown scenarios: {bad_scen} (choose from {list(SCENARIOS)})")
    bad_modes = [m for m in modes if m not in MODES]
    if bad_modes:
        raise SystemExit(f"unknown modes: {bad_modes} (choose from {MODES})")
    bad_audios = [a for a in audios if a not in AUDIOS]
    if bad_audios:
        raise SystemExit(f"unknown audio-confirm: {bad_audios} (choose from {AUDIOS})")

    # One DB per (model, mode): the video-level clip cache is keyed on
    # video_path only, so sharing one DB across models would let one model's
    # audio evidence leak into another's sampled runs.
    conns = {}

    def conn_for(model, mode):
        key = (model, mode)
        if key not in conns:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", model)
            conns[key] = db.connect(work / f"e2e_matrix{suffix}_{safe}_{mode}.db")
        return conns[key]

    # Resumable: completed rows are kept and skipped below; --redo drops only
    # this invocation's own selection. New rows append without doubling.
    results, done = load_prior_rows(out_path, models, only or SLUGS, scen, modes, audios, redo)

    def save():
        out_path.write_text("\n".join(json.dumps(r) for r in results), encoding="utf-8")

    for slug in (only or SLUGS):
        try:
            fx = fixture(slug)
            subs = subs_for(slug, fx)
        except Exception as e:
            print(f"{slug}: skipped ({e})", flush=True)
            continue
        video = media_dir(slug) / fx["video_name"]
        if not video.exists():
            print(f"{slug}: skipped (no video {video.name})", flush=True)
            continue
        # One seeded cache per shard: every row reuses the same WAV entry.
        audio_cache = audio_cache_for(slug, video)
        ref = list(subs.events)
        t0 = time.time()
        for model in models:
            try:
                ev = audio_evidence(model, slug, fx)
            except Exception as e:
                # A sweep file can be half-written while its own producer is
                # still generating -- retry next run, don't kill the shard.
                ev = None
                audio_error = f"unreadable sweep data: {type(e).__name__}: {e}"[:200]
            else:
                audio_error = None
            if ev is None or not ev[1]:
                reason = (audio_error or f"no sweep data: sweep/{model}/{slug}.json missing"
                          if ev is None else
                          f"no usable segments in sweep/{model}/{slug}.json")
                for mode in modes:
                    for audio in audios:
                        for name in scen:
                            _skip_row(results, done, model, slug, name, mode, audio, reason)
                save()
                continue
            lang, segments = ev
            for mode in modes:
                conn = conn_for(model, mode)
                # groq_model carries the sweep config name so the
                # full-transcript cache read (keyed on provider+model) matches
                # this run's priming.
                for audio in audios:
                    cfg = cfg_for(conn, mode, audio, groq_model=model)
                    for name in scen:
                        key = (model, slug, name, mode, audio)
                        if key in done:
                            continue
                        s_rng = random.Random(f"matrix-v1:{slug}:{name}")
                        corrupted, kept, detail = SCENARIOS[name](subs, s_rng)
                        rec = dict(status="ok", model=model, slug=slug, scenario=name,
                                   mode=mode, audio_confirm=audio, detail=detail,
                                   drift_case=slug in DRIFT_CASE_SLUGS)
                        try:
                            row, after = run_one(
                                work, video, corrupted, lang, segments, cfg, conn,
                                f"{slug}.{model}.{mode}.{audio}.{name}", mode,
                                audio_cache)
                            after_ev = list(after.events)
                            rec.update(flag=row.get("correctness_flag"), sync=row.get("sync_status"),
                                       lo_fixed=row.get("line_order_fixed"),
                                       lo_flagged=row.get("line_order_flagged"),
                                       lo_fixed_indices=row.get("lo_fixed_indices", []),
                                       lo_flagged_indices=row.get("lo_flagged_indices", []),
                                       escalated=row.get("escalated", False),
                                       note=(row.get("note") or "")[:200])
                            if name in TIMING_SCENARIOS or name == "clean":
                                rec["injected_p50"] = summarize(
                                    timing_errors(ref, list(corrupted.events), kept)).get("p50")
                                # Drift case: recovery vs its own timings is
                                # meaningless (ref itself drifts from audio).
                                if scores_recovery(slug, name):
                                    rec["recovered"] = summarize(timing_errors(ref, after_ev, kept))
                                if name == "clean":
                                    rec["untouched"] = (
                                        rec["sync"] == "already in sync" and rec["flag"] == "ok"
                                        and rec["lo_fixed"] in (None, 0)
                                        and "SUSPECT" not in (row.get("correctness_flag") or ""))
                            if name in ("swap", "drift_swap"):
                                rec["swap"] = swap_recovery(ref, after_ev, detail.get("swapped", []))
                                rec["swap_detail"] = swap_index_detail(
                                    detail.get("swapped", []), rec["lo_fixed_indices"])
                                rec["swap_noticed"] = bool(
                                    (row.get("line_order_fixed") or 0) > 0
                                    or (row.get("line_order_flagged") or 0) > 0
                                    or row.get("correctness_flag") == "SUSPECT")
                                if name == "drift_swap":
                                    rec["injected_p50"] = summarize(
                                        timing_errors(ref, list(corrupted.events), kept)).get("p50")
                                    if scores_recovery(slug, name):
                                        rec["recovered"] = summarize(
                                            timing_errors(ref, after_ev, kept))
                        except Exception as e:
                            rec["status"] = "error"
                            rec["error"] = f"{type(e).__name__}: {e}"[:200]
                        results.append(rec)
                        done.add(key)
                        r = rec.get("recovered", {})
                        s = rec.get("swap", {})
                        print(f"{slug}.{model}.{name}.{mode}.{audio}: status={rec['status']} "
                              f"rec_p50={r.get('p50')} rec<=1s={r.get('frac_le_1_0s')} "
                              f"restored={s.get('frac_restored')} lo_fixed={rec.get('lo_fixed')} "
                              f"esc={rec.get('escalated')} "
                              f"flag={rec.get('flag')} sync={rec.get('sync')}", flush=True)
                        save()
        print(f"{slug}: done ({time.time()-t0:.0f}s)", flush=True)
        save()
    for conn in conns.values():
        conn.close()
    summary = build_summary(results, models, only or SLUGS, scen, modes, audios)
    (OUT_DIR / f"{out_stem}_summary{suffix}.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    print("wrote", out_path, len(results), "rows")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def build_summary(results, models, slugs, scen, modes=None, audios=None):
    modes = list(modes or MODES)
    audios = list(audios or AUDIOS)
    ok = [r for r in results if r.get("status", "ok") == "ok"]
    by_scenario = {}
    for name in scen:
        rows = [r for r in ok if r["scenario"] == name]
        entry = {
            "runs": len(rows),
            "errors": sum(1 for r in results if r["scenario"] == name and r.get("status") == "error"),
            "skipped": sum(1 for r in results if r["scenario"] == name and r.get("status") == "skipped"),
        }
        if name in TIMING_SCENARIOS or name == "clean":
            rec = [r["recovered"] for r in rows if "recovered" in r and r["recovered"].get("p50") is not None]
            if rec:
                entry["mean_p50"] = round(statistics.mean([x["p50"] for x in rec]), 3)
                entry["mean_frac_le_1s"] = round(statistics.mean([x["frac_le_1_0s"] for x in rec]), 3)
            per_mode = {}
            for m in modes:
                mrec = [r["recovered"] for r in rows
                        if r.get("mode", "full") == m and "recovered" in r
                        and r["recovered"].get("p50") is not None]
                if mrec:
                    per_mode[m] = {
                        "runs": len(mrec),
                        "mean_p50": round(statistics.mean([x["p50"] for x in mrec]), 3),
                        "mean_frac_le_1s": round(
                            statistics.mean([x["frac_le_1_0s"] for x in mrec]), 3),
                    }
            if per_mode:
                entry["by_mode"] = per_mode
        if name == "clean":
            un = [r for r in rows if r.get("untouched")]
            entry["untouched"] = len(un)
        if name in ("swap", "drift_swap"):
            sw = [r["swap"] for r in rows if "swap" in r and r["swap"].get("frac_restored") is not None]
            if sw:
                entry["mean_frac_restored"] = round(
                    statistics.mean([x["frac_restored"] for x in sw]), 3)
            entry["noticed"] = sum(1 for r in rows if r.get("swap_noticed"))
        by_scenario[name] = entry
    by_model = {}
    for model in models:
        rows = [r for r in ok if r["model"] == model]
        entry = {
            "runs": len(rows),
            "errors": sum(1 for r in results if r["model"] == model and r.get("status") == "error"),
            "skipped": sum(1 for r in results if r["model"] == model and r.get("status") == "skipped"),
        }
        rec = [r["recovered"] for r in rows
               if "recovered" in r and r["recovered"].get("frac_le_1_0s") is not None]
        if rec:
            entry["mean_frac_le_1s"] = round(statistics.mean([x["frac_le_1_0s"] for x in rec]), 3)
        sw = [r["swap"] for r in rows if "swap" in r and r["swap"].get("frac_restored") is not None]
        if sw:
            entry["mean_frac_restored"] = round(
                statistics.mean([x["frac_restored"] for x in sw]), 3)
        clean = [r for r in rows if r["scenario"] == "clean"]
        if clean:
            entry["clean_untouched"] = sum(1 for r in clean if r.get("untouched"))
            entry["clean_runs"] = len(clean)
        by_model[model] = entry
    # Cross-model index agreement on clean: which fixed/flagged cue indices
    # all models share (likely genuine) vs singletons (likely artefacts).
    agreement_clean = {}
    for slug in slugs:
        per_slug = {}
        for m in modes:
            per_mode = {}
            for a in audios:
                rows = [r for r in ok if r["scenario"] == "clean" and r["slug"] == slug
                        and r.get("mode", "full") == m and r.get("audio_confirm", "on") == a]
                if len(rows) >= 2:
                    per_mode[a] = {
                        "fixed": agreement([r.get("lo_fixed_indices", []) for r in rows]),
                        "flagged": agreement([r.get("lo_flagged_indices", []) for r in rows]),
                        "mean_lo_fixed": _mean([r.get("lo_fixed") for r in rows]),
                    }
            if per_mode:
                per_slug[m] = per_mode
        if per_slug:
            agreement_clean[slug] = per_slug
    # Swap detail per (mode, audio): injected-index hit rate and the
    # non-injected fix count -- the false-positive side of the same coin.
    swap_detail = {}
    for m in modes:
        for a in audios:
            rows = [r for r in ok if r["scenario"] == "swap"
                    and r.get("mode", "full") == m and r.get("audio_confirm", "on") == a
                    and "swap_detail" in r]
            if rows:
                swap_detail[f"{m}.{a}"] = {
                    "runs": len(rows),
                    "mean_frac_injected_fixed": _mean(
                        [r["swap_detail"].get("frac_injected_fixed") for r in rows]),
                    "mean_n_noninjected_fixed": _mean(
                        [r["swap_detail"].get("n_noninjected_fixed") for r in rows]),
                }
    # clean -> swap delta of lo_fixed per (mode, audio): if the injected swaps
    # were the main thing being fixed, this number should be large; ~0 means
    # the fixer mostly rewrites cues that were already "broken" on clean.
    clean_swap_delta = {}
    for m in modes:
        for a in audios:
            deltas = []
            for model in models:
                for slug in slugs:
                    c = [r for r in ok if r["scenario"] == "clean" and r["model"] == model
                         and r["slug"] == slug and r.get("mode", "full") == m
                         and r.get("audio_confirm", "on") == a]
                    s = [r for r in ok if r["scenario"] == "swap" and r["model"] == model
                         and r["slug"] == slug and r.get("mode", "full") == m
                         and r.get("audio_confirm", "on") == a]
                    if c and s and c[0].get("lo_fixed") is not None \
                            and s[0].get("lo_fixed") is not None:
                        deltas.append(s[0]["lo_fixed"] - c[0]["lo_fixed"])
            if deltas:
                clean_swap_delta[f"{m}.{a}"] = {
                    "pairs": len(deltas), "mean_delta": round(statistics.mean(deltas), 2),
                    "median_delta": round(statistics.median(deltas), 2),
                }
    # Escalation rate per mode: share of sampled runs that went full.
    escalation = {}
    for m in modes:
        rows = [r for r in ok if r.get("mode", "full") == m]
        if rows:
            escalation[m] = {"runs": len(rows),
                             "escalated": sum(1 for r in rows if r.get("escalated"))}
    # Drift case: no recovery means (never scored), only detection signals --
    # did the pipeline notice the genuine drift (SUSPECT flag, sync action)?
    drift_case = {}
    drows = [r for r in ok if r.get("drift_case")]
    if drows:
        by_sc = {}
        for name in scen:
            srows = [r for r in drows if r["scenario"] == name]
            if not srows:
                continue
            syncs = {}
            for r in srows:
                syncs[r.get("sync")] = syncs.get(r.get("sync"), 0) + 1
            by_sc[name] = {
                "runs": len(srows),
                "suspect": sum(1 for r in srows if r.get("flag") == "SUSPECT"),
                "sync_counts": syncs,
            }
        drift_case = {"slugs": sorted({r["slug"] for r in drows}),
                      "runs": len(drows),
                      "errors": sum(1 for r in results if r.get("drift_case")
                                    and r.get("status") == "error"),
                      "skipped": sum(1 for r in results if r.get("drift_case")
                                     and r.get("status") == "skipped"),
                      "by_scenario": by_sc}
    return {"models": models, "slugs": slugs, "scenarios": scen,
            "modes": modes, "audio_confirms": audios,
            "by_scenario": by_scenario, "by_model": by_model,
            "agreement_clean": agreement_clean, "swap_detail": swap_detail,
            "clean_swap_delta": clean_swap_delta, "escalation": escalation,
            "drift_case": drift_case}


if __name__ == "__main__":
    main()
