"""Offset/sync matrix test: corruption scenarios x Whisper-model axis.

Extends tests/e2e_offset.py (which runs piecewise/drift/gap against the
large-v3-turbo fixture only) in two directions: more corruption types, and the
audio evidence coming from a chosen model instead of only the turbo fixture.

Episodes (10: 9 user-confirmed + timing-trusted, plus C_S03E04):
  C_S03E03 C_S03E08 C_S03E10 SH_S01E01 SH_S01E02 SH_S01E03
  SH_S01E04 SH_S01E05 SH_S01E06 (clean facit set)
  C_S03E04 (documented real drift case: user-confirmed correct content
  but -0.048 s/min genuine drift -- see DRIFT_CASE_SLUGS).

Scenarios (23, all in the default set):
  clean          no corruption; control group measuring false positives
                 ("already in sync", no flag, no rewritten timings).
  CONSTANT OFFSET
  uniform        global +45s shift (the old e2e_after.py scenario).
  uniform_neg    -45s: the same size the other way, which the anchor window
                 does NOT see symmetrically (+clip_seconds+30s vs -30s).
  uniform_p03    +0.3s -- just over the 0.25s decision bar; expected to move.
  uniform_m07    -0.7s -- just over it, and negative.
  uniform_p15    +1.5s, uniform_m5  -5s: the rest of the boundary sweep.
  RATE
  fps_late/early 1001/1000 (+/-0.1%): the only rate error measured on the
                 real corpus, in both directions (rapport 9.1).
  drift          generic 2% stretch; matches no named conversion (see its
                 docstring) -- the mid-magnitude point.
  drift_offset   2% stretch PLUS an 8s delay: real conversions carry both,
                 and this is the only scenario that exercises the fitted
                 intercept (corrupt_drift pivots at t=0).
  pal_late/early 25/24 (+/-4.167%): the real PAL speed-up, both directions.
  BLOCK STRUCTURE
  piecewise      6 blocks, each seeded random +/-5..15s.
  piecewise_b/c  the same shape on two further independent draws -- one draw
                 per episode is one draw, and block layout drives outcomes.
  cut_version    the real cut mismatch: middle cues gone AND everything after
                 300s too early. Doing nothing is the WRONG answer here.
  missing_middle 5-minute middle chunk of cues deleted, surviving timings
                 correct. Flagging it WITHOUT rewriting IS the right answer.
                 (Was called "gap"; the old name still resolves.)
  ROBUSTNESS AND SAFETY -- nothing to fix, everything to not break
  dropdup        5% of cues dropped, 5% duplicated in place.
  jitter         per-cue +/-1..3s noise. Pass = no worse, not recovery.
  wrong_episode  another episode's subtitle. Pass = SUSPECT + untouched
                 file; recovery is not scored.
  LINE ORDER
  swap           in-cue L1/L2 line reversal -- see corrupt_swap docstring.
  drift_swap     COMBINED drift (2%) + swap -- a frame-rate-converted file can
                 carry both, and the drift fix and the line-order fix must not
                 interfere with each other.

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
import math
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

from verifyarr import db, generate, line_order, pipeline, sync_engine
from verifyarr.correctness import get_duration_seconds as _get_duration, overrun_evidence as _overrun
from verifyarr.correctness import full_transcript_cache_key
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs, speech_text

FIX = Path("/home/hammer/Auto sync sub/verifyarr/tests/fixtures/whisper_full")
SWEEP = Path("/mnt/c/Users/knham/Desktop/undertekst auto/whisper_gpu_staging/sweep")
DIRS = {
    "C_S02": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 2"),
    "C_S03": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 3"),
    "SH_S01": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Slow Horse/Season 1"),
    # User-confirmed correct episodes from other shows (2026-09-27).
    "KG_": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Known Good"),
}
SWEEP_LINUX = SWEEP.parent / "sweep_linux"
WAV_DIR = Path("/mnt/c/Users/knham/Desktop/undertekst auto/whisper_gpu_staging/wav")
TURBO = "turbo"
SWEEP_MODELS = ["base.en-cpu", "base.en-greedy-cpu", "base.en-q5_1-cpu",
                "medium.en", "medium.en-greedy", "medium.en-q5_0",
                "small.en", "small.en-greedy", "small.en-q5_1",
                "tiny.en-cpu", "tiny.en-greedy-cpu", "tiny.en-q5_1-cpu",
                "turbo-q5_0", "turbo-q8_0"]
ALL_MODELS = [TURBO] + SWEEP_MODELS
# Opt-in via --models: cloud Groq whisper-large-v3-turbo, only for the approved episodes.
OPT_IN_MODELS = ["groq-turbo"]
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
# Randomised families (seeded per scenario name).
BLOCK_SCENARIOS = ({f"block_rand{i}" for i in range(4)}
                   | {f"blocks_rand{i}" for i in range(2)}
                   | {f"cutsteps_rand{i}" for i in range(10)})
HOLE_SCENARIOS = ({f"hole_rand{i}" for i in range(4)}
                  | {f"trunc_start_rand{i}" for i in range(2)}
                  | {f"trunc_end_rand{i}" for i in range(2)})
RATE_SCENARIOS = ({f"drift_rand{i}" for i in range(6)}
                  | {f"ratio_rand{i}" for i in range(4)})
TIMING_SCENARIOS = {"uniform", "uniform_neg", "uniform_p03", "uniform_m07", "uniform_p15",
                    "uniform_m5", "drift", "drift_offset", "pal_late", "pal_early",
                    "piecewise", "piecewise_b", "piecewise_c", "cut_version",
                    "missing_middle", "gap", "drift_swap", "fps_late", "fps_early",
                    "dropdup", "jitter"} | BLOCK_SCENARIOS | RATE_SCENARIOS
# Everything runs by default. Nothing is opt-in any more: fps_late/fps_early used to be
# held out on the grounds that alass already fixed them, but "alass still does it" is
# exactly the kind of assumption that goes stale silently -- and they are the only rate
# error actually measured on the real corpus (rapport 9.1).
#
# Two scenarios are NOT scored on recovery and must not be read as timing failures:
# wrong_episode (pass = SUSPECT + untouched) and swap (pass = lines restored). jitter IS
# scored, but its pass mark is `recovered` no worse than `injected_p50` -- there is no
# systematic error in it to remove.
# Doing nothing is the correct outcome here, so `untouched` is the measurement.
# wrong_episode and jitter are the sharp ones: there is no correction to make, and
# inventing one is worse than reporting the file.
NO_CHANGE_SCENARIOS = {"clean", "missing_middle", "gap", "dropdup", "jitter",
                       "wrong_episode"} | HOLE_SCENARIOS
# Detection-only: untouched is half the answer; the file must ALSO be flagged.
DETECTION_SCENARIOS = {"missing_middle"} | HOLE_SCENARIOS
# Blocks (BLOCK_SCENARIOS): fixed (p50<=0.15, >=98% within 0.5s) OR flagged counts as caught.
DEFAULT_SCENARIOS = ["clean",
                     "uniform", "uniform_neg", "uniform_p03", "uniform_m07",
                     "uniform_p15", "uniform_m5",
                     "fps_late", "fps_early", "drift", "drift_offset",
                     "pal_late", "pal_early",
                     "piecewise", "piecewise_b", "piecewise_c",
                     "cut_version", "missing_middle",
                     "dropdup", "jitter", "wrong_episode",
                     "swap", "drift_swap"]
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
        # Transcribed on Linux with the sweep's flags (tiny: C_S02 rest, Known Good).
        path = SWEEP_LINUX / model / f"{slug}.json"
    if not path.exists():
        return None
    # errors="replace": whisper.cpp writes a truncated multibyte sequence now and
    # then, reproducibly (base.en-greedy-cpu/SH_S01E05 -- 2 bytes inside a hallucinated
    # song lyric). Losing 24 matrix rows over that is worse than one mangled character
    # in one segment's text.
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        print(f"  {model}/{slug}: invalid UTF-8 in sweep output, decoded with replacement",
              flush=True)
    doc = json.loads(text)
    return sweep_language(doc), sweep_segments(doc)


def subs_for(slug, fx):
    return load_subs(media_dir(slug) / fx["subtitle_name"])


def audio_cache_for(slug, video):
    """Pre-seeded alass audio cache for one episode shard.

    The staging WAV is the same 16kHz mono PCM ffmpeg would extract, so
    resolve_alass_reference reuses it with no ffmpeg call. Missing or
    structurally incomplete WAV falls back to an empty cache (ffmpeg
    extraction, as before) -- a truncated staging file must never seed
    a measurement as valid audio."""
    wav = WAV_DIR / f"{slug}.wav"
    if wav.exists() and sync_engine.wav_complete(wav):
        return {video: wav}
    return {}


@contextlib.contextmanager
def patch_whisper_full(lang, segments):
    real = generate.full_transcript_for_check

    def fake(cfg, video_path, tmp_dir, conn, cancel_event=None):
        # Stored like production's _transcribe_or_reuse does: evaluate_against_full_transcript
        # reads the cache, and without this an escalated sampled row could never plan a resync.
        provider, model = full_transcript_cache_key(cfg)
        db.save_full_transcript_cache(conn, video_path, lang, segments,
                                      stt_provider=provider, stt_model=model)
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


_FRESH = {}


def _fresh_conn_cfg(work, key, mode, audio, model, esc_over):
    """One throwaway DB for one row (--fresh-db). Reuses a single path, wiped each
    time, so a 920-row arm leaves one file behind rather than 920."""
    old = _FRESH.pop("conn", None)
    if old is not None:
        with contextlib.suppress(Exception):
            old.close()
    for stale in work.glob("e2e_fresh.db*"):
        with contextlib.suppress(Exception):
            stale.unlink()
    conn = db.connect(work / "e2e_fresh.db")
    _FRESH["conn"] = conn
    return conn, cfg_for(conn, mode, audio, groq_model=model, **esc_over)


LOCAL_VAD_BINARY = Path("/home/hammer/whisper.cpp/build/bin/whisper-vad-speech-segments")
LOCAL_VAD_MODEL = SWEEP.parent / "model" / "ggml-silero-v5.1.2.bin"


def cfg_for(conn, mode="full", audio="on", **over) -> Config:
    cfg = Config.from_db(conn)
    vals = dict(
        backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, line_order_enabled=True,
        line_order_audio_confirm=(audio == "on"), whisper_mode=mode,
        # Local Whisper, like production: the cloud providers only ever generate a missing
        # subtitle from scratch, which the matrix doesn't exercise. The binary just has to exist
        # (has_stt_configured); every call that would run it is patched below.
        local_whisper_binary=sys.executable,
        # Sampled runs the SHIPPED defaults -- nothing tuning-related is pinned here. Pinning
        # made three separate "after" runs read as no-change while the default under test was
        # silently overridden; test_escalation_follows_the_shipped_default guards it now.
        window_minutes=0.5, overlap_threshold=0.25,
    )
    # VAD like the Docker image (binary shipped, Silero model mounted), when present here.
    if LOCAL_VAD_BINARY.exists() and LOCAL_VAD_MODEL.exists():
        vals.update(vad_binary=str(LOCAL_VAD_BINARY), vad_model=str(LOCAL_VAD_MODEL))
    vals.update(over)
    # Name the tested model: transcript cache and hole bar key on it.
    if over.get("groq_model"):
        vals.setdefault("local_whisper_model", f"ggml-{over['groq_model']}.bin")
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
        _provider, _model = full_transcript_cache_key(cfg)
        _db.save_full_transcript_cache(conn, video, lang, segments,
                                       stt_provider=_provider, stt_model=_model)
    tmp = work / f"{tag}.srt"
    subs.save(str(tmp))
    captured = {}
    real_finalize = pipeline.finalize_line_order
    real_apply = pipeline._apply_line_order
    real_screen = pipeline._screen_says_needs_full

    def spy_finalize(collected, cfg_, cancel_event=None, compute_swap_severity=True):
        res = real_finalize(collected, cfg_, cancel_event=cancel_event,
                            compute_swap_severity=compute_swap_severity)
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

    def spy_screen(collected, cfg_, *a, **kw):
        hit = real_screen(collected, cfg_, *a, **kw)
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
        def _run():
            # Same order as pipeline.process_pair: Whisper screens the file BEFORE alass, and
            # can end it there. Calling sync_pair directly here is what made the first framerate
            # build look dead -- the harness must exercise the real chain, not a shortcut.
            screen = pipeline.screen_pair(video, tmp, "en", cfg, conn)
            r, c = pipeline.sync_pair(video, tmp, "en", cfg, audio_cache, work, screen=screen)
            return pipeline.correctness_and_finish(video, tmp, "en", cfg, conn, r, c), c

        with patch_whisper_full(lang, segments):
            if mode == "sampled":
                with patch_sampled_transcription(lang, segments):
                    row, cur = _run()
            else:
                row, cur = _run()
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
    """Global constant shift. A negative shift DROPS the cues it would push before
    zero rather than clamping them there: an SRT cannot hold a negative timestamp
    (pysubs2 raises TimestampUnderflow), and clamping would quietly turn a constant
    offset into a piecewise one at the head of the file -- a different scenario
    measuring a different thing. Dropped cues come back as kept indices."""
    out = copy.deepcopy(subs)
    kept, events = [], []
    for i, e in enumerate(out.events):
        start = int(e.start + shift_s * 1000)
        if start < 0:
            continue
        e.start, e.end = start, int(e.end + shift_s * 1000)
        kept.append(i)
        events.append(e)
    out.events = events
    dropped = len(subs.events) - len(kept)
    return (out, (kept if dropped else None),
            {"shift_s": shift_s, "dropped_before_zero": dropped})


def _shift_block(events, b0: float, b1: float, shift: float) -> None:
    """Moves every cue starting in [b0, b1) seconds by shift, in place."""
    for e in events:
        if b0 <= e.start / 1000.0 < b1:
            e.start = max(0, int(e.start + shift * 1000))
            e.end = max(e.start + 200, int(e.end + shift * 1000))


def _fit_start(start: float, length: float, dur: float) -> float:
    """A span start pulled back so the span ends inside the episode."""
    return max(0.0, dur - length) if start + length > dur else start


def _dialogue_removed(subs, kept) -> int:
    """Removed cues that carry spoken words: sound descriptions and song lines
    ("[GUNSHOTS]", "♪ ... ♪") are not dialogue and never count."""
    keep = set(kept)
    return sum(1 for i, e in enumerate(subs.events)
               if i not in keep and speech_text(e.plaintext).split())


def _drop_span(out, g0: float, g1: float) -> list[int]:
    """Removes cues lying wholly in [g0, g1] seconds; returns kept indices."""
    kept = [i for i, e in enumerate(out.events)
            if not (g0 * 1000 <= e.start and e.end <= g1 * 1000)]
    out.events = [out.events[i] for i in kept]
    return kept


def corrupt_piecewise(subs, rng, n_blocks=6, lo=5.0, hi=15.0):
    """Each contiguous time block shifted by its own random +/-lo..hi seconds."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    shifts = []
    for b in range(n_blocks):
        mag = rng.uniform(lo, hi) * rng.choice((-1, 1))
        shifts.append(mag)
        _shift_block(out.events, dur * b / n_blocks, dur * (b + 1) / n_blocks, mag)
    return out, None, {"block_shifts_s": [round(s, 2) for s in shifts]}


def corrupt_drift(subs, rng, rate=0.02):
    """Progressive stretch t -> t*(1+rate): a generic rate error, 25s end-to-end on a
    21-minute episode.

    2% matches no named conversion -- it was once labelled "25<->23.976fps style", which
    is +4.27%, not 2%. The real conversions are corrupt_pal_late/early (25/24, +4.167%)
    and corrupt_fps_late/early (1001/1000, +0.1%); this one is kept as the mid-magnitude
    point between them, not as a claim about any real pipeline."""
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(e.start * (1 + rate))
        e.end = int(e.end * (1 + rate))
    return out, None, {"rate": rate}


# 24 fps subtitle on a 23.976 fps video (or the reverse). Exactly 1001/1000 = +0.1%,
# i.e. 0.06 s/min -- 20x smaller than corrupt_drift, and the size actually measured on the
# real corpus: all four "drifting" episodes are this, in both directions (rapport 9.1).
NTSC_RATIO = 1001 / 1000.0


def _rescale(subs, ratio):
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(round(e.start * ratio))
        e.end = int(round(e.end * ratio))
    return out, None, {"ratio": round(ratio, 6)}


def corrupt_fps_late(subs, rng):
    """Subtitle progressively later: t -> t*1001/1000."""
    return _rescale(subs, NTSC_RATIO)


def corrupt_fps_early(subs, rng):
    """Subtitle progressively earlier: t -> t*1000/1001. The other direction is real too
    (C_S02E11), and a corrector that only handles one of them is half a corrector."""
    return _rescale(subs, 1 / NTSC_RATIO)


def corrupt_missing_middle(subs, rng, gap_seconds=300.0):
    """Delete a contiguous middle chunk of CUES, leaving every surviving timing correct.

    Named for what it is. It was called "gap (cut version)", which it is not: a cut
    version also moves everything after the cut (see corrupt_cut_version). Here the
    right answer is to flag the hole and leave the file alone -- 5 minutes where
    Whisper hears dialogue and the file has none. Returns kept indices."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    g0, g1 = dur * 0.4, dur * 0.4 + gap_seconds
    kept = _drop_span(out, g0, g1)
    return out, kept, {"gap_start_s": round(g0, 1), "gap_end_s": round(g1, 1),
                       "removed_dialogue": _dialogue_removed(subs, kept),
                       "removed_lines": len(subs.events) - len(kept)}


def corrupt_swap(subs, rng, n=6):
    """Reverse L1/L2 inside up to n two-line cues.

    Chosen failure mode: in-cue line reversal is exactly what
    verifyarr/line_order.py detects and auto-fixes (free cap-signal prefilter
    + Whisper audio confirmation in _judge_order, mechanical fix in
    line_order.py). Whole-file cue reordering is NOT used instead: it would
    defeat content matching everywhere at once (a wrong-episode verdict, not a
    line-order fix), which is a different feature's job.

    Targets mirror tests/test_sync_verification.py::LineOrderTests: only cues
    whose SWAPPED form trips _cap_signal (so each injected swap is actually
    testable -- swapping already-lowercase/uppercase pairs would be invisible
    by construction, not a detection failure), excluding cues the heuristic
    already flags in the original. Targets are spread evenly across the whole
    candidate list rather than taken as the first n: the first n are all in the
    opening minutes, so every swap landed in one clump and nothing tested whether
    detection holds up late in a file. Still deterministic, no rng needed.
    """
    from verifyarr.line_order import _split_two_lines, _cap_signal, heuristic_candidates
    out = copy.deepcopy(subs)
    already = {c[0] for c in heuristic_candidates(out)}
    cands = [i for i, e in enumerate(out.events)
             if (p := _split_two_lines(e.text)) and _cap_signal(p[1], p[0])
             and i not in already]
    if len(cands) > n:
        step = len(cands) / n
        targets = [cands[int(k * step)] for k in range(n)]
    else:
        targets = cands
    for i in targets:
        l1, l2 = _split_two_lines(out.events[i].text)
        out.events[i].text = f"{l2}\\N{l1}"
    return out, None, {"swapped": targets}


def corrupt_drift_swap(subs, rng, rate=0.02, n=6):
    """Combined drift + in-cue swap: a frame-rate-converted file can carry both
    at once (timing skewed AND lines misordered), so the two fixes must not
    fight each other."""
    drifted, _, d_detail = corrupt_drift(subs, rng, rate=rate)
    out, kept, s_detail = corrupt_swap(drifted, rng, n=n)
    return out, kept, {"rate": rate, "swapped": s_detail["swapped"]}


def corrupt_many_swaps(subs, rng, frac=0.25):
    """25 % of two-line cues reversed: the many-swaps file the gate must flag.

    Cap-signal-tripping targets first (like corrupt_swap), then any two-line
    cue to reach the fraction. Deterministic, no rng needed."""
    from verifyarr.line_order import (_split_two_lines, _cap_signal,
                                      heuristic_candidates, all_two_line_events)
    out = copy.deepcopy(subs)
    already = {c[0] for c in heuristic_candidates(out)}
    cands = [i for i, e in enumerate(out.events)
             if (p := _split_two_lines(e.text)) and _cap_signal(p[1], p[0])
             and i not in already]
    n = max(1, int(len(all_two_line_events(out)) * frac))
    if len(cands) < n:
        extra = [i for i, e in enumerate(out.events)
                 if _split_two_lines(e.text) and i not in already
                 and i not in cands]
        cands = cands + [i for i in extra if i not in cands]
    targets = cands[:n] if len(cands) <= n else [
        cands[int(k * len(cands) / n)] for k in range(n)]
    for i in targets:
        l1, l2 = _split_two_lines(out.events[i].text)
        out.events[i].text = f"{l2}\\N{l1}"
    return out, None, {"swapped": targets}


# The real PAL speed-up: 24fps film run at 25fps, +4.167%. This is the most common
# rate error in the wild and the one alass guesses at unprompted (it applied 25/24 to a
# 2% stretch and turned it into 6.25% -- rapport 10.5). corrupt_drift's 2% matches no
# named conversion; these two do, and they sit under STRETCH_MAX_RATE (0.08) so the
# measured-rate fix has to reach them.
PAL_RATIO = 25 / 24.0


def corrupt_pal_late(subs, rng):
    """24fps subtitle on a 25fps (PAL) video: t -> t*25/24, +4.167%."""
    return _rescale(subs, PAL_RATIO)


def corrupt_pal_early(subs, rng):
    """The reverse: a PAL subtitle on the 24fps master, t -> t*24/25."""
    return _rescale(subs, 1 / PAL_RATIO)


def corrupt_drift_offset(subs, rng, rate=0.02, offset_s=8.0):
    """Stretch AND a constant delay, which is what a real conversion carries: the file
    was re-timed and then muxed against a differently-trimmed master.

    corrupt_drift pivots exactly at t=0, so its fitted intercept is always ~0 and the
    intercept half of the correction is never exercised. It is not decoration: leaving
    it out left a late cue 19s off on C_S02E01 (see pipeline._try_stretch_rescale)."""
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(e.start * (1 + rate) + offset_s * 1000)
        e.end = int(e.end * (1 + rate) + offset_s * 1000)
    return out, None, {"rate": rate, "offset_s": offset_s}


def corrupt_cut_version(subs, rng, cut_seconds=300.0):
    """A subtitle from a CUT broadcast against the uncut video -- the real cut-version
    mismatch, which corrupt_missing_middle only looks like.

    Two things go wrong at once, and the second is the hard one: the cues covering the
    cut are absent, AND every cue after it sits cut_seconds too EARLY, because in the
    broadcast that material started that much sooner. So this is a genuine two-block
    problem with a 300s step in the middle -- alass' home ground, and a case where
    leaving the file alone (half the answer for missing_middle) is the wrong answer."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    c0, c1 = dur * 0.4, dur * 0.4 + cut_seconds
    kept, events = [], []
    for i, e in enumerate(out.events):
        if c0 * 1000 <= e.start and e.end <= c1 * 1000:
            continue
        if e.start / 1000.0 >= c1:
            e.start = max(0, int(e.start - cut_seconds * 1000))
            e.end = max(e.start + 200, int(e.end - cut_seconds * 1000))
        kept.append(i)
        events.append(e)
    out.events = events
    return out, kept, {"cut_start_s": round(c0, 1), "cut_end_s": round(c1, 1),
                       "removed_lines": len(subs.events) - len(kept),
                       "post_cut_shift_s": -cut_seconds}


def corrupt_hole_random(subs, rng, lo_s=60.0, hi_s=300.0):
    """Random hole 60-300s at 10-90%: same shape as missing_middle, new draw."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    size = rng.uniform(lo_s, hi_s)
    g0 = _fit_start(dur * rng.uniform(0.10, 0.90), size, dur)
    g1 = g0 + size
    kept = _drop_span(out, g0, g1)
    return out, kept, {"gap_start_s": round(g0, 1), "gap_end_s": round(g1, 1),
                       "size_s": round(size, 1),
                       "removed_dialogue": _dialogue_removed(subs, kept),
                       "removed_lines": len(subs.events) - len(kept)}


def corrupt_trunc_start_random(subs, rng, lo_s=60.0, hi_s=300.0):
    """Random 60-300s cut off the head: download stopped early at the start."""
    out = copy.deepcopy(subs)
    removed = rng.uniform(lo_s, hi_s)
    t0 = min(e.start for e in out.events) / 1000.0
    kept = [i for i, e in enumerate(out.events)
            if e.start / 1000.0 >= t0 + removed]
    out.events = [out.events[i] for i in kept]
    return out, kept, {"removed_s": round(removed, 1),
                       "removed_dialogue": _dialogue_removed(subs, kept),
                       "removed_lines": len(subs.events) - len(kept)}


def corrupt_trunc_end_random(subs, rng, lo_s=60.0, hi_s=300.0):
    """Random 60-300s cut off the tail: download stopped early at the end."""
    out = copy.deepcopy(subs)
    removed = rng.uniform(lo_s, hi_s)
    dur = max(e.end for e in out.events) / 1000.0
    kept = [i for i, e in enumerate(out.events)
            if e.start / 1000.0 < dur - removed]
    out.events = [out.events[i] for i in kept]
    return out, kept, {"removed_s": round(removed, 1),
                       "removed_dialogue": _dialogue_removed(subs, kept),
                       "removed_lines": len(subs.events) - len(kept)}


def corrupt_block_random(subs, rng, lo_len=90.0, hi_len=600.0, lo_shift=2.0, hi_shift=20.0):
    """One random block 90-600s shifted +/-2-20s, anywhere incl. edges."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    length = rng.uniform(lo_len, hi_len)
    b0 = _fit_start(dur * rng.uniform(0.0, 0.95), length, dur)
    shift = rng.uniform(lo_shift, hi_shift) * rng.choice((-1, 1))
    _shift_block(out.events, b0, b0 + length, shift)
    return out, None, {"block_start_s": round(b0, 1), "length_s": round(length, 1),
                       "shift_s": round(shift, 2)}


def _stretch_offset(subs, rate, offset_s):
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = max(0, int(e.start * (1 + rate) + offset_s * 1000))
        e.end = max(e.start + 200, int(e.end * (1 + rate) + offset_s * 1000))
    return out


def corrupt_drift_random(subs, rng, lo=0.0015, hi=0.05, max_offset_s=10.0):
    """Any rate 0.15-5% (log-uniform, either sign) plus an offset up to +/-10s."""
    rate = math.exp(rng.uniform(math.log(lo), math.log(hi))) * rng.choice((-1, 1))
    offset = rng.uniform(-max_offset_s, max_offset_s)
    return _stretch_offset(subs, rate, offset), None, {
        "rate": round(rate, 5), "offset_s": round(offset, 2)}


# Real conversions: 23.976<->24, 24<->25 (PAL), 23.976<->25.
REAL_RATIOS = (1001 / 1000, 25 / 24, 25 / (24000 / 1001))


def corrupt_ratio_random(subs, rng, max_offset_s=10.0):
    """A real conversion ratio, random direction, plus an offset up to +/-10s."""
    r = rng.choice(REAL_RATIOS)
    r = r if rng.random() < 0.5 else 1 / r
    offset = rng.uniform(-max_offset_s, max_offset_s)
    return _stretch_offset(subs, r - 1, offset), None, {
        "rate": round(r - 1, 5), "offset_s": round(offset, 2)}


def corrupt_blocks_random(subs, rng, n_lo=2, n_hi=3):
    """2-3 non-overlapping random blocks, each 90-300s shifted +/-2-20s."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    n = rng.randint(n_lo, n_hi)
    blocks, tries = [], 0
    while len(blocks) < n and tries < 50:
        tries += 1
        length = rng.uniform(90.0, 300.0)
        b0 = _fit_start(dur * rng.uniform(0.0, 0.95), length, dur)
        if any(b0 < hi + 30 and b0 + length + 30 > lo for lo, hi, _ in blocks):
            continue
        blocks.append((b0, b0 + length, rng.uniform(2.0, 20.0) * rng.choice((-1, 1))))
    for b0, b1, shift in blocks:
        _shift_block(out.events, b0, b1, shift)
    detail = {"blocks": [{"start_s": round(b0, 1), "length_s": round(b1 - b0, 1),
                          "shift_s": round(s, 2)} for b0, b1, s in blocks]}
    return out, None, detail


def corrupt_cutsteps_random(subs, rng, n_lo=1, n_hi=3, lo_s=2.0, hi_s=120.0):
    """Commercial breaks: the subtitle was timed to a version with 1-3 breaks the video
    no longer has (or the reverse). Everything after each break moves by its length,
    so the offsets add up to the end of the file -- a staircase, not an island."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    sign = rng.choice((-1, 1))
    cuts = sorted((dur * rng.uniform(0.10, 0.90), sign * rng.uniform(lo_s, hi_s))
                  for _ in range(rng.randint(n_lo, n_hi)))
    for t, shift in cuts:
        _shift_block(out.events, t, dur + 1.0, shift)
    return out, None, {"cuts": [{"at_s": round(t, 1), "shift_s": round(s, 2)} for t, s in cuts]}


def corrupt_dropdup(subs, rng, frac=0.05):
    """5% of cues dropped and 5% duplicated in place -- merge/OCR damage. The timings
    that survive are CORRECT, so the pass mark is that nothing is broken: no crash, no
    index drift, and no correction invented for a file that needs none.

    `kept` carries the reference index of every surviving cue in OUTPUT order, so a
    duplicate scores against the line it copies instead of shifting everything after
    it by one (timing_errors pairs after_events[j] with ref_events[kept[j]])."""
    out = copy.deepcopy(subs)
    kept, events, dropped, duped = [], [], 0, 0
    for i, e in enumerate(out.events):
        r = rng.random()
        if r < frac:
            dropped += 1
            continue
        events.append(e)
        kept.append(i)
        if r > 1 - frac:
            events.append(copy.deepcopy(e))
            kept.append(i)
            duped += 1
    out.events = events
    return out, kept, {"dropped": dropped, "duplicated": duped}


def corrupt_jitter(subs, rng, lo=1.0, hi=3.0):
    """Per-cue random +/-1..3s with no systematic shape: OCR and live-caption noise.

    Nothing here is recoverable -- there is no single offset, rate or block structure to
    find. The pass mark is therefore NOT recovery: it is that `recovered` comes back no
    worse than `injected_p50`. A pipeline that chases this noise into pieces has failed
    even if some cues improve."""
    out = copy.deepcopy(subs)
    for e in out.events:
        d = rng.uniform(lo, hi) * rng.choice((-1, 1))
        e.start = max(0, int(e.start + d * 1000))
        e.end = max(e.start + 200, int(e.end + d * 1000))
    return out, None, {"jitter_s": [lo, hi]}


def corrupt_wrong_episode(subs, rng, slug):
    """Another episode's subtitle against this video. Nothing to recover and nothing to
    score on timing -- the pass mark is a SUSPECT flag and an UNTOUCHED file.

    tests/test_sync_verification.py::WrongEpisodeTests covers the property once; here it
    gets the model x mode variation, because "we never rewrite a file we cannot verify"
    is a safety property and safety properties are where model strength matters most."""
    others = [s for s in SLUGS if s != slug]
    for other in (others[rng.randrange(len(others)):] + others):
        try:
            return copy.deepcopy(subs_for(other, fixture(other))), None, {"from_slug": other}
        except Exception:   # a slug whose fixture is missing must not kill the row
            continue
    raise RuntimeError("no other episode available for wrong_episode")


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


def _uniform(shift_s):
    return lambda subs, rng: corrupt_uniform(subs, rng, shift_s=shift_s)


SCENARIOS = {
    "clean": corrupt_clean,
    # Constant offsets. uniform (+45s) is the alass smoke test; uniform_neg is the same
    # size the other way, which is NOT symmetric for us -- the anchor window reaches
    # clip_seconds + 30s forward but only 30s back (line_order.py), so a late subtitle
    # is visible to the screen where an early one of the same size is not.
    "uniform": corrupt_uniform, "uniform_neg": _uniform(-45.0),
    # The decision boundary is 0.25s (min_change_seconds = SCREEN_TOLERANCE_S):
    # below it nothing should move at all. Both signs, both sides, deliberately
    # small. uniform_p03 (+0.3s) sits just ABOVE it, so it is expected to move.
    "uniform_p03": _uniform(0.3), "uniform_m07": _uniform(-0.7),
    "uniform_p15": _uniform(1.5), "uniform_m5": _uniform(-5.0),
    # Rate errors, smallest to largest: 0.1% (measured real), 2% (generic), 4.167% (PAL).
    "fps_late": corrupt_fps_late, "fps_early": corrupt_fps_early,
    "drift": corrupt_drift, "drift_offset": corrupt_drift_offset,
    "pal_late": corrupt_pal_late, "pal_early": corrupt_pal_early,
    # Block structure. Three seeds because one draw per episode is one draw: the rng is
    # keyed on slug+scenario, so distinct names give independent block layouts.
    "piecewise": corrupt_piecewise, "piecewise_b": corrupt_piecewise,
    "piecewise_c": corrupt_piecewise,
    "cut_version": corrupt_cut_version,
    "missing_middle": corrupt_missing_middle,
    # Robustness and safety: nothing to fix, everything to not break.
    "dropdup": corrupt_dropdup, "jitter": corrupt_jitter,
    "wrong_episode": corrupt_wrong_episode,
    "swap": corrupt_swap, "drift_swap": corrupt_drift_swap,
    "many_swaps": corrupt_many_swaps,
    # Randomized sizes/positions (seeded per slug+scenario, like piecewise_b/c).
    # Opt-in via --scenarios; DEFAULT_SCENARIOS stays fixed for comparability.
    "hole_rand0": corrupt_hole_random, "hole_rand1": corrupt_hole_random,
    "hole_rand2": corrupt_hole_random, "hole_rand3": corrupt_hole_random,
    "trunc_start_rand0": corrupt_trunc_start_random,
    "trunc_start_rand1": corrupt_trunc_start_random,
    "trunc_end_rand0": corrupt_trunc_end_random,
    "trunc_end_rand1": corrupt_trunc_end_random,
    "block_rand0": corrupt_block_random, "block_rand1": corrupt_block_random,
    "block_rand2": corrupt_block_random, "block_rand3": corrupt_block_random,
    "blocks_rand0": corrupt_blocks_random, "blocks_rand1": corrupt_blocks_random,
    **{f"drift_rand{i}": corrupt_drift_random for i in range(6)},
    **{f"ratio_rand{i}": corrupt_ratio_random for i in range(4)},
    **{f"cutsteps_rand{i}": corrupt_cutsteps_random for i in range(10)},
    # Old name kept so historical commands and jsonl comparisons still resolve.
    "gap": corrupt_missing_middle,
}


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
    # --fresh-db: one throwaway DB per ROW instead of one per (model, mode) shard.
    #
    # The shared DB is not a detail. The clip cache is keyed on (video_path,
    # region_index, STT provider, STT model), so every row for an episode inherits
    # the clips its predecessors bought -- and a sampled row that would see 18
    # anchors on its own sees 46 after a dozen earlier scenarios primed the same
    # video. Measured: SH_S01E06 drift sampled flips from warned (fresh) to fixed
    # (primed). Production meets each file once. So the shared-DB number is the
    # optimistic one and this is the honest one; keep both, and never quote the
    # primed figure as what a first run will do.
    fresh_db = "--fresh-db" in argv
    # Escalation policy under test: how many clips must show a real residual before the whole
    # track gets transcribed (see sync.escalate_min_bad_samples), and --no-escalate to switch
    # the whole ladder off. Escalation is ~10x the audio a sampled run otherwise spends.
    esc_over = {}
    if "--escalate-min-bad" in argv:
        esc_over["escalate_min_bad_samples"] = int(argv[argv.index("--escalate-min-bad") + 1])
    if "--no-escalate" in argv:
        esc_over["escalate_sampled_to_full"] = False
    if "--suspect-min" in argv:
        esc_over["anchor_suspect_min_samples"] = int(argv[argv.index("--suspect-min") + 1])
    if "--escalate-any-block" in argv:
        esc_over["escalate_only_multi_block"] = False
    if "--escalate-on" in argv:
        esc_over["escalate_sampled_to_full"] = True
    if "--sample-count" in argv:
        esc_over["sample_count"] = int(argv[argv.index("--sample-count") + 1])
    if "--clips-per-10" in argv:
        esc_over["clips_per_10min"] = float(argv[argv.index("--clips-per-10") + 1])
    if "--clip-seconds" in argv:
        esc_over["clip_seconds"] = int(argv[argv.index("--clip-seconds") + 1])
    if "--fps-sampled-fix" in argv:
        esc_over["fps_require_full_coverage"] = False
    suffix = f"_{shard}" if shard else ""
    work = OUT_DIR / f"e2e_work_matrix{suffix}"
    work.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{out_stem}{suffix}.jsonl"
    bad_models = [m for m in models if m not in ALL_MODELS + OPT_IN_MODELS]
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

    # One DB per (model, mode): belt and braces now that the video-level clip
    # cache is keyed on STT provider+model too -- sharing one DB across models
    # used to let one model's audio evidence leak into another's sampled runs.
    #
    # --redo drops the DBs for what it re-runs. Without this the caches outlive the run and a
    # "before/after" comparison silently mixes fresh results with the previous run's cached
    # correctness/line-order evidence -- measured: 105 rows changed on code paths the change
    # under test could not reach.
    if redo:
        for model in models:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", model)
            for mode in modes:
                for stale in work.glob(f"e2e_matrix{suffix}_{safe}_{mode}.db*"):
                    stale.unlink()
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
                    cfg = cfg_for(conn, mode, audio, groq_model=model, **esc_over)
                    for name in scen:
                        key = (model, slug, name, mode, audio)
                        if key in done:
                            continue
                        if fresh_db:
                            conn, cfg = _fresh_conn_cfg(work, key, mode, audio,
                                                        model, esc_over)
                        s_rng = random.Random(f"matrix-v1:{slug}:{name}")
                        # wrong_episode is the one corruption that needs a second
                        # episode, so it takes the slug to know which one NOT to use.
                        corrupted, kept, detail = (
                            corrupt_wrong_episode(subs, s_rng, slug)
                            if name == "wrong_episode"
                            else SCENARIOS[name](subs, s_rng))
                        rec = dict(status="ok", model=model, slug=slug, scenario=name,
                                   mode=mode, audio_confirm=audio, detail=detail,
                                   drift_case=slug in DRIFT_CASE_SLUGS)
                        try:
                            row, after = run_one(
                                work, video, corrupted, lang, segments, cfg, conn,
                                f"{slug}.{model}.{mode}.{audio}.{name}", mode,
                                audio_cache)
                            after_ev = list(after.events)
                            rec.update(flag=row.get("correctness_flag"), reason=row.get("reason"),
                                       sync=row.get("sync_status"),
                                       lo_fixed=row.get("line_order_fixed"),
                                       lo_flagged=row.get("line_order_flagged"),
                                       lo_fixed_indices=row.get("lo_fixed_indices", []),
                                       lo_flagged_indices=row.get("lo_flagged_indices", []),
                                       swap_rate=row.get("line_order_swap_rate"),
                                       whisper_cost=row.get("whisper_cost"),
                                       escalated=row.get("escalated", False),
                                       # 200 chars cut off mid-sentence before the resync/
                                       # re-verdict clauses, which is where a half-right fix
                                       # explains itself. Diagnosis needs the whole note.
                                       note=(row.get("note") or "")[:2000],
                                       # [[sample_start, anchored?], ...] on the FINAL file.
                                       # A half-right fix leaves its broken stretch unanchored,
                                       # so where the gaps sit is the evidence; counts are not.
                                       anchor_map=[[s.get("start"), int(s.get("anchor") is not None),
                                                    round((s.get("anchor") or {}).get("shift", 0.0), 2),
                                                    (s.get("anchor") or {}).get("mad")]
                                                   for s in (row.get("correctness_samples") or [])],
                                       # [[sample_start, content score], ...] on the FINAL file.
                                       score_map=[[s.get("start"), s.get("score"), s.get("sub_tokens")]
                                                  for s in (row.get("correctness_samples") or [])])
                            # Speech past the audio end, injected file vs final file.
                            _dur = _get_duration(video)
                            rec["overrun_in"] = _overrun(corrupted, _dur)
                            rec["overrun_out"] = _overrun(after, _dur)
                            if name in TIMING_SCENARIOS or name == "clean":
                                rec["injected_p50"] = summarize(
                                    timing_errors(ref, list(corrupted.events), kept)).get("p50")
                                # Drift case: recovery vs its own timings is
                                # meaningless (ref itself drifts from audio).
                                if scores_recovery(slug, name):
                                    rec["recovered"] = summarize(timing_errors(ref, after_ev, kept))
                            if name in NO_CHANGE_SCENARIOS:
                                # Scenarios where doing nothing is the right answer, so
                                # "was the file left alone" is the measurement -- not just
                                # for clean: a wrong-episode subtitle we rewrite is worse
                                # than one we merely fail to fix.
                                # "left unchanged" counts as untouched too: it is what a
                                # rejected alass fit leaves behind, and on wrong_episode
                                # it is the CORRECT outcome alongside a SUSPECT flag. So
                                # the flag is reported separately rather than folded in.
                                _s = rec["sync"] or ""
                                rec["untouched"] = (
                                    (_s.startswith("already in sync")
                                     or _s.startswith("left unchanged"))
                                    and rec["lo_fixed"] in (None, 0))
                            if name in DETECTION_SCENARIOS:
                                rec["detected"] = (rec.get("flag") != "ok"
                                                   and bool(rec.get("untouched")))
                            if name in BLOCK_SCENARIOS:
                                _rec = rec.get("recovered") or {}
                                _fixed = (_rec.get("p50") is not None
                                          and _rec["p50"] <= 0.15
                                          and _rec.get("frac_le_0_5s", 0) >= 0.98)
                                rec["caught"] = bool(_fixed or rec.get("flag") != "ok")
                            if name in ("swap", "drift_swap", "many_swaps"):
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
        if name in DETECTION_SCENARIOS:
            entry["detected"] = sum(1 for r in rows if r.get("detected"))
        if name in BLOCK_SCENARIOS:
            entry["caught"] = sum(1 for r in rows if r.get("caught"))
        if name in ("swap", "drift_swap", "many_swaps"):
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
