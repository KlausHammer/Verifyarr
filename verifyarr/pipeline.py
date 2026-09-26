"""Core per-file flow — process_pair runs sync (alass) + correctness check (Whisper) for one
video/subtitle pair, and handle_suspect deals with the result if it gets flagged SUSPECT.
Split into sync_pair (alass only, no DB access) + correctness_and_finish (Whisper + DB) so
jobs._run_sweep can run the sync half across a thread pool while keeping the correctness half
sequential — process_pair itself just calls the two back to back, for callers that don't need
them split (CLI, the Bazarr-hook single-file path, remediate's own candidate verification)."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import shutil
import statistics
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr.memo import BoundedMemo
from verifyarr import vad as vad_timeline
from verifyarr.settings import Config
from verifyarr.subtitles import (
    load_subs, max_shift_stats, summarize_anchor_samples, ANCHOR_PREFER_MARGIN_S,
    ANCHOR_SUSPECT_THRESHOLD_S, plan_anchor_resync, apply_anchor_resync,
    anchor_spread, ANCHOR_SCREEN_SPREAD_S, vad_tilt_from_intervals, apply_fps_rescale, _spread,
    stretch_probe, STRETCH_MIN_POINTS, STRETCH_MIN_TILT_S, STRETCH_MAX_RATE,
    STRETCH_RHO_MIN, STRETCH_MIN_GAIN_S, STRETCH_MAX_RESID_S, STRETCH_MIN_KEEP_FRAC,
    anchor_drift_signature, max_quartile_residual_after, FPS_RESID_MAX_S,
    FPS_RATIOS, FPS_ANCHOR_TILT_MIN_S, FPS_BINNED_TILT_MIN_S, FPS_LOO_TILT_MIN_S,
    FPS_VAD_TILT_MIN_S, FPS_MIN_ANCHORS,
    FPS_MAX_BASE_SPREAD_S, rate_gates_pass, rate_is_flat, snap_rate, RATE_MIN_TILT_S,
    probe_gates_pass, stretch_ratio, stretch_name,
)
from verifyarr.sync_engine import (
    resolve_alass_bin, resolve_alass_reference, run_alass, parse_alass_shift_blocks,
    parse_alass_shift_blocks_with_counts,
)
from verifyarr.line_order import (
    heuristic_candidates, collect_samples, collect_samples_full, finalize_line_order, cache_key_for,
    swap_gate_evidence, swap_gate_trips, SWAP_GATE_ESCALATE_HEURISTIC,
)
from verifyarr.correctness import (
    evaluate_against_cached_transcripts, evaluate_against_full_transcript, significant_anchor_residuals, JobCancelled,
    whisper_cost, anchor_slope_breaks, anchor_run_offsets, get_duration_seconds,
    anchor_jitter, JITTER_MIN_MAD_S, JITTER_ESCALATE_MAD_S,
    ANCHOR_RESYNC_INTERVAL_S, ANCHOR_SUSPECT_MIN_SAMPLES,
    all_gaps, missing_middle_evidence, MISSING_MIDDLE_ESCALATE_GAP_S,
    gap_probe_windows, gap_speech, clears_missing_middle, missing_middle_min_words,
    anchor_block_clusters, anchor_point_runs, dense_anchor_points,
    full_transcript_cache_key,
)
# Same filters full_transcript_for_check applies on the way out: the cache holds raw
# segments, and an unfiltered repetition loop reads as a scene's worth of dialogue.
from verifyarr.generate import _drop_nonspeech, _drop_repetition_loops
from verifyarr.fileops import backup_subtitle, quarantine_subtitle
from verifyarr.bazarr import (
    bazarr_map_path, bazarr_blacklist, remediate_suspect, remediate_without_history,
    request_replacement_fire_and_forget,
)
from verifyarr import db
from verifyarr.db import update_state


def _try_quarantine(subtitle_path: Path, cfg: Config, media_root: Path):
    """Quarantine move that never raises: (dest, None) on success, (None, error text) on
    failure. The three handle_suspect branches below share this so the move-vs-report
    shape exists once — each branch keeps its own message and logging, since those
    deliberately differ ("quarantine failed" vs "blacklist failed, and could not
    quarantine either")."""
    try:
        return quarantine_subtitle(subtitle_path, cfg.quarantine_dir, media_root), None
    except Exception as e:
        return None, str(e)


def handle_suspect(subtitle_path: Path, video_path: Path, cfg: Config, media_root: Path,
                    lang: Optional[str], bazarr_meta: Optional[dict],
                    history_index: Optional[dict], auto_action: str,
                    conn: Optional[sqlite3.Connection] = None,
                    run_id: Optional[int] = None, cancel_event=None) -> str:
    """auto_action: off | quarantine | blacklist | remediate — passed in by the caller, which in
    practice always means correctness_auto_action. Line-order deliberately has no equivalent: a
    confirmed swap is a safe mechanical per-line fix, so it is never on its own a reason to throw
    the file away and fetch another release.

    blacklist/remediate hand the file to Bazarr's own blacklist endpoint rather than moving it
    out first: Bazarr deletes the file itself as part of blacklisting it, and -- ONLY if that
    delete succeeds -- automatically searches for and starts fetching a replacement. Moving the
    file away beforehand (the old behavior) made that delete fail every time (file already
    gone), silently breaking Bazarr's own auto-redownload. So for these two actions we instead
    leave the file in place for Bazarr to remove, and back up a copy first (general.
    backup_originals) as the local safety net for that deletion instead of a quarantine move."""
    if auto_action == "off":
        return "none (action=off)"
    if cfg.dry_run:
        return f"would {auto_action} [dry-run]"

    if auto_action == "quarantine":
        dest, error = _try_quarantine(subtitle_path, cfg, media_root)
        if error is not None:
            log.warning("Could not quarantine %s: %s", subtitle_path, error)
            return f"quarantine failed: {error}"
        return f"quarantined -> {dest}"

    meta = dict(bazarr_meta) if bazarr_meta else None
    if meta is None and history_index is not None:
        meta = history_index.get(bazarr_map_path(cfg, subtitle_path))
    if meta is None:
        # No Bazarr HISTORY match -- there's nothing to hand Bazarr's blacklist endpoint (no
        # provider/subs_id to identify a "current source" with), so it can't remove/replace the
        # file itself the normal way. Quarantine the bad file locally either way -- but for
        # remediate specifically, still try to get a REPLACEMENT via a manual provider search if
        # Bazarr's own catalog at least knows this episode's ID (from an earlier whole-library
        # scan/Detect now -- see db.get_bazarr_ids_for_video). Most common real cause: the
        # current subtitle came bundled with the original release rather than through Bazarr at
        # all, or its own history entry is already blacklisted from an earlier run (excluded
        # from the lookup on purpose -- see bazarr_build_history_index).
        dest, error = _try_quarantine(subtitle_path, cfg, media_root)
        if error is not None:
            log.warning("Could not quarantine %s: %s", subtitle_path, error)
            return f"quarantine failed: {error}"
        msg = f"quarantined -> {dest}; blacklist skipped (no Bazarr match)"
        if conn is not None and auto_action in ("remediate", "blacklist"):
            ids = db.get_bazarr_ids_for_video(conn, video_path)
            if ids and ids.get("kind") == "series" and ids.get("series_id") and ids.get("episode_id"):
                if auto_action == "remediate":
                    msg += "; " + remediate_without_history(
                        video_path, cfg, lang, ids["series_id"], ids["episode_id"],
                        cancel_event=cancel_event, conn=conn, run_id=run_id)
                else:
                    msg += "; " + request_replacement_fire_and_forget(
                        cfg, ids["series_id"], ids["episode_id"], lang)
        return msg

    meta.setdefault("subtitles_path", bazarr_map_path(cfg, subtitle_path))
    if not meta.get("language") and lang:
        meta["language"] = lang

    if cfg.backup_originals:
        try:
            backup_subtitle(subtitle_path, cfg.backup_dir, media_root)
        except Exception as e:
            log.warning("Could not back up %s before blacklisting: %s", subtitle_path, e)

    ok = bazarr_blacklist(cfg, meta)
    if not ok:
        # Bazarr didn't remove it (couldn't reach it, etc.) -- fall back to moving it out of
        # the library ourselves rather than leaving a known-bad file in place doing nothing.
        dest, error = _try_quarantine(subtitle_path, cfg, media_root)
        if error is not None:
            return f"blacklist failed, and could not quarantine either: {error}"
        result = f"blacklist failed; quarantined -> {dest} instead"
        if auto_action == "blacklist" and meta.get("kind") != "movie" and meta.get("series_id") and meta.get("episode_id"):
            result += "; " + request_replacement_fire_and_forget(cfg, meta["series_id"], meta["episode_id"], lang)
        return result

    msg = "blacklisted in Bazarr (file removed there; Bazarr is searching for a replacement)"
    if conn is not None:
        db.add_blacklist_action(
            conn, subtitle_path=str(subtitle_path), video_path=str(video_path), kind=meta.get("kind", "episode"),
            provider=meta.get("provider"), subs_id=meta.get("subs_id"), language=meta.get("language"),
            series_id=meta.get("series_id"), episode_id=meta.get("episode_id"), radarr_id=meta.get("radarr_id"),
            run_id=run_id,
        )

    if auto_action != "remediate":
        return msg
    return msg + "; " + remediate_suspect(subtitle_path, video_path, cfg, media_root, lang, meta,
                                           cancel_event=cancel_event, conn=conn, run_id=run_id)


# Below this nothing is worth rewriting a file for. A subtitle that is off by less than
# a quarter of a second everywhere is in sync as far as a viewer is concerned, and the
# user's rule is explicit: a small drift, or a couple of cues off by under 0.25s, is not
# worth fixing. Every screen threshold is this same number -- offset, block spread and
# accumulated drift -- because they are all "how far is a cue from where it belongs", just
# measured differently. Same value as sync.min_change_seconds' default: the screen and the
# write gate are one rule, coded in two places (see tests/test_sync_threshold.py).
SCREEN_TOLERANCE_S = 0.25
SCREEN_MIN_CLIPS = 3          # fewer confident clips than this is not evidence, it is silence
# Of the clips that produced a match, how many land on the SAME answer. The gate on applying
# a global offset before alass, and the reason is a measured near-miss: piecewise seed 0 read
# as a clean -5.41s offset with a spread of 0.23s, because the clips that matched all sat in
# one block. Measured over 10 episodes x 6 scenarios (agreement among matched clips):
#   clean 0.56-1.00, gap 0.60-1.00, uniform 0.67-1.00, piecewise 0.00-0.73, drift 0.00-0.33
# 0.80 clears piecewise by 0.07 and keeps the three uniform files whose -45s shift is real.
# Deliberately NOT applied to the "ok" verdict: that rests on offset, spread and drift all
# being small, which caught 109 of 109 damaged files in a separate 1070-measurement sweep.
SCREEN_MIN_AGREE_FRAC = 0.80
# A subtitle that ends this long before the audio is not vouched for by clips that all fell
# before its end: 52 real episodes end at most 55s early (credits); a cut broadcast's
# subtitle ends the cut's length sooner. Defers to the normal chain, never a flag.
SCREEN_MAX_TAIL_GAP_S = 150.0


def screen_pair(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
                conn: sqlite3.Connection, cancel_event=None) -> dict:
    """screen_pair proper (_screen_pair) plus its own Whisper bill, measured as a DELTA.

    It cannot reset the module-level counter: a sweep screens every file in its batch before
    any correctness check runs (jobs._run_sweep), so a reset here wipes the previous file's
    total and each row ends up reporting the last screened file's audio plus everything
    charged since. The counter's docstring says it is safe because one file runs at a time;
    that stopped being true when Whisper moved ahead of alass. So: measure the delta, hand it
    to the row, and let correctness_and_finish keep resetting for its own phase."""
    before = whisper_cost.snapshot()
    out = _screen_pair(video_path, subtitle_path, lang, cfg, conn, cancel_event=cancel_event)
    after = whisper_cost.snapshot()
    out["cost"] = {k: round(after.get(k, 0.0) - before.get(k, 0.0), 1) for k in after}
    return out


def _screen_pair(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
                 conn: sqlite3.Connection, cancel_event=None) -> dict:
    """Whisper evidence BEFORE alass, so we can answer "does this file need anything at all"
    and stop there when the answer is no.

    Why this order. alass runs first today, and on a stretched file it guesses a framerate
    conversion that is not there: a 2% stretch comes out the other side as 6.25% (measured on
    C_S03E03, findings 10.5). Everything downstream then measures alass' damage instead of the
    original error. Measured on 12 episodes, share of cues landing within 1s of the truth:
    alass alone 0.082, this screen's own correction 1.000 on all 12. Running alass afterwards
    added nothing (median 0.30s against 0.14s) and destroyed one file outright.

    What it costs. The clips go into the same video-level cache the correctness check reads
    (db.save_transcript_cache, keyed on video+region+STT model, never on the subtitle), so
    when the verdict IS "needs work" the fixing pass reuses this audio instead of buying it
    again. A healthy file additionally skips alass entirely -- and with it the full audio
    track extraction alass needs, which is ~6.5s here and several times that on an N100.

    Returns {"verdict", "shift", "spread", "tilt", "n_clips", "collected"}:
      "ok"         every clip agrees, within SCREEN_TOLERANCE_S, and there is no drift
      "needs_sync" something moved: the caller runs the normal repair chain
      "unknown"    not enough evidence to say (too few clips matched, or the check is off) --
                   treated as "needs_sync" by callers, never as "ok"
    """
    if not cfg.enable_correctness_check or not cfg.has_stt_configured:
        return {"verdict": "unknown", "reason": "correctness check unavailable"}
    try:
        subs = load_subs(subtitle_path)
    except Exception as e:
        return {"verdict": "unknown", "reason": f"could not parse subtitle: {e}"}
    with tempfile.TemporaryDirectory() as td:
        # Full mode already owns the whole transcript: screening from clips there would buy
        # audio it has, and would judge on a thinner pool than the evidence that follows.
        if cfg.whisper_mode == "full":
            collected = collect_samples_full(video_path, subs, lang, cfg, Path(td),
                                             conn, cancel_event=cancel_event)
        else:
            collected = collect_samples(video_path, subs, lang, cfg, Path(td),
                                        conn=conn, cancel_event=cancel_event)
    if collected.get("skipped"):
        return {"verdict": "unknown", "reason": collected.get("reason"), "collected": None}
    shifts = [s["anchor"]["shift"] for s in (collected.get("samples") or [])
              if s.get("anchor") and s["anchor"].get("shift") is not None]
    # Every clip we actually spent audio on, matched or not -- the denominator for "does one
    # answer explain this file". A silence-skip is not a failed match and does not count.
    attempted = [s for s in (collected.get("samples") or [])
                 if not (s.get("error") or "").startswith("VAD silence-skip")]
    out = {"collected": collected, "n_clips": len(shifts), "n_attempted": len(attempted),
           "shift": None, "spread": None, "tilt": None,
           "agree_frac": None, "match_frac": None}
    if len(shifts) < SCREEN_MIN_CLIPS:
        # Not silence-as-proof: a uniformly shifted file matches NOTHING (measured: 0 anchors
        # on 35 of 35 injected +45s files), and so does a wrong-episode subtitle. Both have to
        # go on to the repair chain, not be waved through.
        out["verdict"] = "unknown"
        out["reason"] = f"only {len(shifts)} clip(s) matched"
        return out
    out["shift"] = statistics.median(shifts)
    out["spread"] = _spread(shifts) if len(shifts) >= 3 else None
    out["agree_frac"] = (sum(1 for x in shifts if abs(x - out["shift"]) < SCREEN_TOLERANCE_S)
                         / len(shifts))
    out["match_frac"] = len(shifts) / max(1, len(attempted))
    probe = stretch_probe([(a, a - s) for a, s in _fps_points(collected)])
    out["tilt"] = (probe or {}).get("tilt")
    over = [abs(out["shift"]) >= SCREEN_TOLERANCE_S,
            out["spread"] is not None and out["spread"] >= SCREEN_TOLERANCE_S,
            out["tilt"] is not None and abs(out["tilt"]) >= SCREEN_TOLERANCE_S]
    out["verdict"] = "needs_sync" if any(over) else "ok"
    if out["verdict"] == "ok":
        # Median/MAD ignore a minority by design; a block IS a minority.
        # No verdict here -- just run alass, which decides what moves.
        if anchor_block_clusters(collected.get("samples") or []):
            out["verdict"] = "needs_sync"
            out["reason"] = "off-median anchor cluster (possible block)"
    if out["verdict"] == "ok" and subs.events:
        duration = get_duration_seconds(video_path)
        tail = duration - max(e.end for e in subs.events) / 1000.0 if duration else None
        if tail is not None and tail > SCREEN_MAX_TAIL_GAP_S:
            out["verdict"] = "unknown"
            out["reason"] = f"subtitle ends {tail:.0f}s before the audio"
    return out


def presync_from_screen(subs, screen: dict, cfg: Config):
    """The correction the screen itself can describe, applied BEFORE alass runs.

    Only two shapes, because only two are safe to take from sampled evidence without alass'
    own opinion: a whole-file rate error (stretch_probe's gates -- see _try_stretch_rescale)
    and a constant offset every clip agrees on. Anything block-shaped is left entirely to
    alass, which is good at it and which we are not.

    The point is not to replace alass. It is that alass measures against the file it is given,
    and on a stretched file it guesses a framerate conversion that is not there -- 2% comes out
    as 6.25% (C_S03E03). Hand it a file whose rate is already right and it produces one clean
    block instead of three broken ones (measured: recovery 0.00 -> 1.00).

    Returns (fixed_subs, description) or None. Never writes; the caller owns the file.
    """
    pts = _fps_points(screen.get("collected") or {})
    probe = _dense_probe(pts)
    if probe is not None and _stretch_gates_pass(probe):
        ratio = stretch_ratio(probe)
        fixed, worst = _rescaled(subs, ratio, probe["intercept"])
        if worst >= cfg.min_change_seconds:
            return fixed, (f"rate {(ratio - 1) * 100:+.2f}% "
                           f"({probe['n']} anchors, {probe['keep_frac']:.0%} on the line)")
    shift = screen.get("shift")
    spread = screen.get("spread")
    # A constant offset is only constant if the clips agree it is. Anything else is blocks.
    if (shift is not None and abs(shift) >= max(cfg.min_change_seconds, SCREEN_TOLERANCE_S)
            and spread is not None and spread < SCREEN_TOLERANCE_S
            and (screen.get("agree_frac") or 0) >= SCREEN_MIN_AGREE_FRAC
            and (screen.get("tilt") is None or abs(screen["tilt"]) < SCREEN_TOLERANCE_S)):
        fixed = copy.deepcopy(subs)
        for e in fixed.events:
            e.start = max(0, int(round(e.start + shift * 1000)))
            e.end = max(e.start, int(round(e.end + shift * 1000)))
        return fixed, (f"offset {shift:+.2f}s ({screen.get('n_clips')} of "
                       f"{screen.get('n_attempted')} clips agreeing, spread {spread:.2f}s)")
    return None


def process_pair(video_path: Path, subtitle_path: Path, lang: Optional[str],
                  cfg: Config, conn: sqlite3.Connection,
                  bazarr_meta: Optional[dict] = None,
                  history_index: Optional[dict] = None,
                  audio_cache: Optional[dict] = None,
                  audio_cache_dir: Optional[Path] = None,
                  run_id: Optional[int] = None,
                  cancel_event=None) -> dict:
    """Sync (alass) + correctness check (Whisper), sequentially, for one video/subtitle pair —
    a thin wrapper over sync_pair + correctness_and_finish for callers that don't need the two
    stages split apart: the CLI, the Bazarr-hook single-file path (jobs._run_single), and
    remediate's own candidate verification. See jobs._run_sweep for the split, parallel-sync
    version used by a sweep."""
    # Whisper first, alass second (see screen_pair): the screen can end the file here, and when
    # it cannot, its clips are already cached for everything downstream.
    screen = (screen_pair(video_path, subtitle_path, lang, cfg, conn, cancel_event=cancel_event)
              if cfg.sync_enabled else None)
    row, current_subs = sync_pair(video_path, subtitle_path, lang, cfg, audio_cache, audio_cache_dir,
                                  screen=screen)
    return correctness_and_finish(video_path, subtitle_path, lang, cfg, conn, row, current_subs,
                                   bazarr_meta=bazarr_meta, history_index=history_index,
                                   run_id=run_id, cancel_event=cancel_event)


# How far alass may move a freshly GENERATED subtitle before that is worth flagging. Its cues
# were timed from this very video's audio, so the honest expectation is a shift near zero; a
# large one means the transcription's own timeline was wrong (badly chunked audio, a mis-detected
# spoken language), not that the subtitle was out of sync. Deliberately loose -- Whisper's segment
# timestamps are only approximate, and alass legitimately takes up a second or two of slack.
GENERATED_SYNC_WARN_SECONDS = 5.0


def finish_generated(video_path: Path, subtitle_path: Path, cfg: Config, conn: sqlite3.Connection,
                      row: dict, run_id: Optional[int] = None) -> dict:
    """Persists the result of sync_pair for a subtitle generate.generate_one just wrote --
    WITHOUT running the ordinary Whisper correctness check (correctness_and_finish). Skipping it
    is deliberate, not a shortcut: the correctness check exists to catch "does this subtitle
    actually match what's said in THIS video" -- a failure mode that can't happen for a subtitle
    built directly from that same video's own Whisper transcript, translated or not (translation
    only changes the wording, never which video/timestamps the text came from). Running it anyway
    would just spend real API quota re-confirming something already guaranteed by construction.

    Sync (alass) still runs, via the caller's own sync_pair call before this -- cheap (local,
    no API cost) and still a genuine check of OUR OWN output (a timestamp/format bug in
    build_srt_from_segments/write_new_subtitle), unlike the correctness check's redundant
    re-verification of content. See jobs._run_generate_single, the only caller.

    Note what skipping the check does NOT cover, since "guaranteed by construction" is only true
    of the content: nothing here can see a Whisper hallucination, a mistimed chunk, or a failed
    translation. Those are caught earlier instead, where there is actually evidence to catch them
    with -- generate._low_confidence_reason drops segments Whisper itself was unsure of, and
    generate.translate_segments refuses to write a partly-translated file at all. The alass shift
    below is the one signal available at THIS stage, and a big one is now surfaced rather than
    quietly applied: a subtitle timed from this video's own audio should already line up with it,
    so alass disagreeing by a lot means one of the two is wrong."""
    apply_pending_sync(subtitle_path, cfg, row, reason="no correctness check runs for a generated subtitle")
    # Not "-" (never checked) and not None: its own flag, so Files/Stats can tell a generated
    # subtitle apart from one whose check merely hasn't run yet, and so nothing reads the absence
    # of a SUSPECT verdict here as a passed check.
    row["correctness_flag"] = "generated"
    note = (row.get("note") or "") + (
        " Correctness check skipped -- generated directly from this video's own Whisper "
        "transcription.")
    shift = row.get("sync_max_shift_s")
    if shift is not None and abs(shift) > GENERATED_SYNC_WARN_SECONDS:
        note += (f" NOTE: alass moved this generated subtitle by {shift:.1f}s. Its timings came "
                 "from this video's own audio, so a shift that large points at a transcription "
                 "problem (badly chunked audio, or a mis-detected spoken language) rather than at "
                 "a subtitle that was merely out of sync -- worth checking by hand.")
        log.warning("Generated subtitle %s needed a %.1fs alass shift — that should be near zero "
                    "for a subtitle timed from this video's own audio; check it manually.",
                    subtitle_path.name, shift)
    row["note"] = note.strip()
    update_state(conn, video_path, subtitle_path, row, run_id=run_id, media_root=cfg.media_root_for(subtitle_path))
    # The 'missing' placeholder for this (video, lang) is now satisfied -- without this it lives
    # on next to the real row forever (different unique index, see db.mark_missing/clear_missing),
    # keeping a "Generate" button on the Files page for a video that just got exactly what it
    # asked for.
    db.clear_missing(conn, video_path, row.get("lang"))
    return row


def _block_time_ranges(subs, blocks_detailed: list[tuple[int, float]]) -> list[tuple[float, float]]:
    """(start_sec, end_sec) per alass block, IN THE AUDIO's OWN TIMELINE -- reconstructed from
    `subs`' (the ORIGINAL subtitle's) events plus each block's own shift. alass's stderr only
    ever says how many CONSECUTIVE events a block covers and by how much it moved them (see
    sync_engine.parse_alass_shift_blocks_with_counts), never an absolute start/end. Walks the
    events in chronological order (sorted defensively -- a well-formed SRT is already in order,
    but nothing here should silently mis-map blocks if one isn't) `count` at a time, then
    applies the block's shift so the range says where in the AUDIO that block's dialogue is --
    which is what a targeted Whisper sample has to be extracted from (see collect_samples'
    extra_target_ranges), regardless of which candidate's cue timing is used to pick the
    dialogue-dense point inside it."""
    events = sorted(subs, key=lambda e: e.start)
    ranges = []
    idx = 0
    for count, shift in blocks_detailed:
        block_events = events[idx:idx + count]
        idx += count
        if block_events:
            ranges.append((max(0.0, min(e.start for e in block_events) / 1000.0 + shift),
                            max(e.end for e in block_events) / 1000.0 + shift))
    return ranges


def _backup_best_effort(subtitle_path: Path, cfg: Config, media_root: Path) -> None:
    """Backs the original up when configured, but never lets a failed backup block the fix
    itself -- an unwritable backup_dir would otherwise silently stop every sync/line-order write
    (found when backup_originals became the default). Same policy handle_suspect already uses."""
    if not cfg.backup_originals:
        return
    try:
        backup_subtitle(subtitle_path, cfg.backup_dir, media_root)
    except Exception as e:
        log.warning("Could not back up %s (continuing without a backup): %s", subtitle_path, e)


def _write_fix(subtitle_path: Path, cfg: Config, media_root: Path, source) -> None:
    """Puts a sync result on disk, backing the original up first when configured. `source` is
    either alass's own output file (a temp Path, copied verbatim) or an already-parsed
    pysubs2 file (saved via pysubs2)."""
    _backup_best_effort(subtitle_path, cfg, media_root)
    if isinstance(source, Path):
        shutil.copyfile(source, subtitle_path)
    else:
        source.save(str(subtitle_path))


def _below_min_change(cfg: Config, shift) -> bool:
    """A fit smaller than min_change_seconds isn't worth writing. Only safe where nothing has
    been put on disk yet -- skipping the write then leaves the original untouched."""
    return shift is not None and abs(shift) < cfg.min_change_seconds


def apply_pending_sync(subtitle_path: Path, cfg: Config, row: dict, reason: str):
    """Fallback for a row whose sync_pair DEFERRED its fix (row["_ambiguous_sync"], see
    sync_pair) but whose caller then turned out unable to run the Whisper-based comparison that
    was supposed to settle it (_resolve_ambiguous_sync) -- correctness check off/unavailable,
    ffprobe failing, the audio not in the required language, the job cancelled, ... Applies
    the SAFE default (alass's single-offset fit, the same thing sync_pair would have written
    outright had there been nothing to compare) so the file never stays unsynced on disk with
    a "[pending verification]" status nobody will come back to, and so the internal pysubs2
    objects never leak into what gets persisted/serialized (reports.write_report). No-op when
    nothing is pending. Returns the subs written, or None."""
    # Only correctness_and_finish reads these; every other path drops them here.
    row.pop("_pre_sync_subs", None)
    row.pop("_orig_subs", None)
    ambiguous = row.pop("_ambiguous_sync", None)
    if ambiguous is None:
        return None
    new_subs = ambiguous["new_subs"]
    max_shift_new = ambiguous["max_shift_new"]
    if _below_min_change(cfg, max_shift_new):
        row["sync_status"] = "already in sync"
        row["sync_max_shift_s"] = round(max_shift_new, 2)
        row["sync_split_blocks"] = None
        row["sync_block_spread_s"] = None
        row["note"] += (f" alass suggested Δ{max_shift_new:.2f}s, under the "
                        f"{cfg.min_change_seconds}s threshold — left as is ({reason}).")
        # Disk holds the original when presync fired; baseline never left temp.
        return ambiguous["orig_subs"]
    _write_fix(subtitle_path, cfg, cfg.media_root_for(subtitle_path), new_subs)
    row["sync_status"] = f"fixed (Δ{max_shift_new:.1f}s)"
    row["sync_max_shift_s"] = round(max_shift_new, 2) if max_shift_new is not None else None
    row["sync_split_blocks"] = 1
    row["sync_block_spread_s"] = None
    if ambiguous.get("single_block"):
        row["note"] += (f" Single-offset fit couldn't be verified against the audio "
                        f"({reason}) — applied unverified.")
        return new_subs
    spread = ambiguous.get("blocks_spread")
    row["note"] += (f" alass also found a {ambiguous.get('blocks_split_count') or '?'}-block fit"
                    + (f" (spread {spread:.1f}s)" if spread is not None else "")
                    + f", but it couldn't be verified against the audio ({reason}) — applied the "
                    f"single-offset fit unverified.")
    return new_subs


def sync_pair(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
              audio_cache: Optional[dict] = None, audio_cache_dir: Optional[Path] = None,
              audio_cache_lock=None, defer_verification: bool = True,
              screen: Optional[dict] = None) -> tuple[dict, object]:
    """Sync stage only (alass) — the first half of what process_pair used to do in one piece.
    No `conn`/DB access at all, which is exactly what makes it safe to run from a worker thread
    (see jobs._run_sweep's parallel sync phase — alass itself is single-threaded per invocation,
    verified against its own Cargo.toml, so running several at once is what actually uses more
    than one of the NAS's cores). correctness_and_finish below picks up from here, sequentially.

    alass runs in its normal split-penalty mode first (the only mode there was before the
    verified-second-opinion feature). A MULTI-block result is structurally suspicious (alass
    can fit mismatched content in pieces just as happily as it fits real cuts -- see
    sync_engine.parse_alass_shift_blocks), so a second, --no-split run is made: a single-offset
    fit that can't overfit in pieces. Both are then held back from disk (row["_ambiguous_sync"])
    for correctness_and_finish to compare against the pre-alass baseline on real audio content
    (_resolve_ambiguous_sync) before anything is written -- reusing the Whisper samples the
    correctness check pays for anyway. A single-block fit is held back the same way (it IS the
    single-offset fit already, so no second run): content must speak before any write, since a
    confident-looking single offset on wrong-episode text is exactly as meaningless as a
    multi-block one. That deferral only happens when the comparison can actually run
    (correctness check on, API key present, not a dry-run, and defer_verification -- a caller
    that will never run correctness_and_finish passes False, e.g. jobs._run_generate_single);
    otherwise the fit is applied directly, exactly as before the feature existed, and
    correctness_and_finish's block-spread safety net still watches it.

    Returns (row, current_subs). current_subs is None only when the ORIGINAL subtitle file
    itself couldn't even be parsed — there's nothing for correctness_and_finish to check either
    in that case; it just persists `row` as-is and moves on.

    audio_cache_lock: see sync_engine.resolve_alass_reference — pass one only when this may run
    concurrently with other sync_pair calls sharing the same audio_cache dict, so two languages
    of the same video don't race to extract/overwrite its cached audio at once."""
    row = {
        "video": str(video_path), "subtitle": str(subtitle_path), "lang": lang or "",
        "sync_status": "-", "sync_max_shift_s": None, "structural_change": False,
        "sync_split_blocks": None, "sync_block_spread_s": None,
        "correctness_flag": "-", "correctness_avg_score": None,
        "line_order_fixed": None, "line_order_flagged": None,  # None = not checked (feature off)
        "note": "",
        "auto_action": "-",
    }
    try:
        old_subs = load_subs(subtitle_path)
    except Exception as e:
        row["sync_status"] = "parse-error"
        row["note"] = str(e)
        return row, None

    current_subs = old_subs
    media_root = cfg.media_root_for(subtitle_path)

    if screen is not None:
        # Carried to correctness_and_finish (_row_cost), which adds it to this file's bill.
        row["_screen_cost"] = screen.get("cost")

    alass_bin = resolve_alass_bin()
    if not cfg.sync_enabled:
        # Settings -> Automation "What runs" table (sync.enabled), further narrowed for an
        # auto-triggered run (the Bazarr wanted-subtitles poll) by auto_scan_sync_enabled — see
        # jobs._effective_cfg. Ahead of the screen's early return on purpose: a screened-clean
        # file must still report that sync was off, not "already in sync", because nothing
        # compared it to the audio on the sync side at all.
        row["sync_status"] = "skipped (disabled in settings)"
        return row, current_subs

    # Whisper already looked at this file (screen_pair) and every clip agreed it is where it
    # belongs. Stop here: no alass, and therefore not the full audio track extraction alass
    # needs either -- the single most expensive step on a slow machine. The clips the screen
    # bought are cached per video, so the correctness check that follows re-reads them for free.
    if screen is not None and screen.get("verdict") == "ok":
        row["sync_status"] = "already in sync"
        row["sync_max_shift_s"] = round(abs(screen["shift"]), 2) if screen.get("shift") is not None else None
        row["note"] = (f" Screened on {screen.get('n_clips')} Whisper clips before sync: "
                       f"offset {screen['shift']:+.2f}s"
                       + (f", spread {screen['spread']:.2f}s" if screen.get("spread") is not None else "")
                       + (f", drift {screen['tilt']:+.2f}s" if screen.get("tilt") is not None else "")
                       + f" -- all under {SCREEN_TOLERANCE_S}s, so alass was not run.")
        return row, current_subs

    if not alass_bin:
        row["sync_status"] = "alass not found"
        return row, current_subs

    with tempfile.TemporaryDirectory() as td:
        # Our own correction FIRST, when the screen could describe one (a whole-file rate or a
        # constant offset every clip agrees on). alass then measures a file whose shape is
        # already right, instead of inventing a framerate conversion to explain the ramp --
        # see presync_from_screen. Written to a temp file, never to disk: alass' output is
        # what gets applied, and if alass fails nothing has been touched.
        presync = presync_from_screen(old_subs, screen, cfg) if screen else None
        alass_input = subtitle_path
        presync_note = ""
        if presync is not None:
            presynced, presync_desc = presync
            alass_input = Path(td) / f"presynced{subtitle_path.suffix}"
            presynced.save(str(alass_input))
            current_subs = presynced
            presync_note = f" Pre-sync before alass: {presync_desc}."
        # The best state we have WITHOUT alass. Everything downstream that means "what we would
        # keep if alass turns out to be wrong" means this, not the untouched original: the
        # pre-sync is a measured correction, not a guess to fall back from.
        baseline_subs = presync[0] if presync is not None else old_subs

        reference_path = resolve_alass_reference(video_path, audio_cache, audio_cache_dir, audio_cache_lock)
        tmp_primary = Path(td) / f"synced{subtitle_path.suffix}"
        ok, msg, stderr_tail = run_alass(alass_bin, reference_path, alass_input, tmp_primary, cfg.split_penalty)
        # old_subs, not current_subs: the pre-sync only ever reached a temp file, so when alass
        # fails the bytes on disk are still the original. Returning the pre-synced object would
        # have the correctness check -- and the line-order cache key it writes -- describe
        # timings no file has.
        if not ok:
            row["sync_status"] = f"error: {msg}"
            row["note"] = (stderr_tail[:300] + presync_note).strip()
            return row, old_subs
        try:
            primary_subs = load_subs(tmp_primary)
        except Exception as e:
            row["sync_status"] = "could not parse alass output"
            row["note"] = (str(e) + presync_note).strip()
            return row, old_subs

        # Against the ORIGINAL, not against the pre-synced file: the report should say how far
        # the cues moved in total, and the min_change gate below should judge the whole move.
        max_shift, _avg_shift, old_n, new_n = max_shift_stats(old_subs, primary_subs)
        row["sync_max_shift_s"] = round(max_shift, 2) if max_shift is not None else None
        structural = bool(old_n) and abs(old_n - new_n) / old_n > 0.1
        row["structural_change"] = structural
        structural_note = " line count changed significantly — check the file manually." if structural else ""

        shift_blocks = parse_alass_shift_blocks(stderr_tail)
        row["sync_split_blocks"] = len(shift_blocks)
        spread = None
        blocks_note = ""
        if len(shift_blocks) > 1:
            spread = round(max(shift_blocks) - min(shift_blocks), 2)
            row["sync_block_spread_s"] = spread
            blocks_note = (f" alass used {len(shift_blocks)} sync blocks with shifts "
                           f"{[round(s, 1) for s in shift_blocks]}s (spread {spread:.1f}s) — can be caused "
                           f"by real cuts in the episode, but can also be a sign of a wrong subtitle, "
                           f"check manually.")

        if max_shift is None or max_shift < cfg.min_change_seconds:
            row["sync_status"] = "already in sync"
            row["note"] += presync_note + blocks_note
            # Disk still holds the original: presync never left its temp file.
            return row, old_subs if presync is not None else current_subs

        # Use the corrected timing for the correctness check either way — even during dry-run,
        # where nothing is written to disk yet, but the report should still reflect what WOULD
        # happen. Otherwise Whisper audio gets compared against the old, wrong timing, producing
        # false SUSPECT flags on exactly the files with the biggest sync error.
        current_subs = primary_subs
        if cfg.dry_run:
            row["sync_status"] = f"would fix (Δ{max_shift:.1f}s) [dry-run]"
            row["note"] += presync_note + blocks_note + structural_note
            return row, current_subs

        can_verify = (len(shift_blocks) > 1 and defer_verification
                      and cfg.enable_correctness_check and cfg.has_stt_configured)
        can_verify_single = (len(shift_blocks) == 1 and defer_verification
                             and cfg.enable_correctness_check and cfg.has_stt_configured)
        if can_verify:
            tmp_single = Path(td) / f"synced_single{subtitle_path.suffix}"
            ok2, _msg2, _stderr2 = run_alass(alass_bin, reference_path, alass_input, tmp_single,
                                             cfg.split_penalty, no_splits=True)
            single_subs = None
            if ok2:
                try:
                    single_subs = load_subs(tmp_single)
                except Exception:
                    single_subs = None
            if single_subs is not None:
                max_shift_single, *_ = max_shift_stats(old_subs, single_subs)
                # Held back from disk: correctness_and_finish decides which of {baseline, this
                # single-offset fit, the multi-block fit} actually scores best against the real
                # audio (see _resolve_ambiguous_sync) before anything gets written. The
                # single-offset fit is the candidate the correctness check itself runs on (the
                # safe default); the block ranges let it aim extra samples at every block.
                # orig_subs is the bytes on disk (baseline is presynced when presync fired).
                row["_ambiguous_sync"] = {
                    "old_subs": baseline_subs, "orig_subs": old_subs,
                    "had_presync": presync is not None,
                    "new_subs": single_subs, "max_shift_new": max_shift_single,
                    "blocks_subs": primary_subs, "max_shift_blocks": max_shift,
                    "blocks_split_count": len(shift_blocks), "blocks_spread": spread,
                    "blocks_time_ranges": _block_time_ranges(
                        old_subs, parse_alass_shift_blocks_with_counts(stderr_tail)),
                    "structural": structural,
                }
                current_subs = single_subs
                shift_txt = f"{max_shift_single:.1f}" if max_shift_single is not None else "?"
                row["sync_status"] = f"fixed (Δ{shift_txt}s) [pending verification]"
                row["sync_max_shift_s"] = round(max_shift_single, 2) if max_shift_single is not None else None
                row["sync_split_blocks"] = 1
                row["sync_block_spread_s"] = None
                # The pre-sync already corrected alass' input file (all three candidates
                # below build on it) -- without this the note never says so whenever
                # alass goes multi-block (measured: SH_S01E06 full drift fixed by an
                # invisible presync, note reads as alass-only).
                row["note"] += presync_note
                return row, current_subs
            blocks_note += " (alass's --no-split alternative failed, so this multi-block fit was applied unverified.)"
        if can_verify_single:
            # Primary IS the single-offset fit: no second alass run, candidates are
            # {baseline, primary}. Empty ranges force the content branch below.
            row["_ambiguous_sync"] = {
                "old_subs": baseline_subs, "orig_subs": old_subs,
                "had_presync": presync is not None,
                "new_subs": primary_subs, "max_shift_new": max_shift,
                "blocks_split_count": 1, "blocks_spread": None,
                "blocks_time_ranges": [],
                "structural": structural, "single_block": True,
            }
            row["sync_status"] = f"fixed (Δ{max_shift:.1f}s) [pending verification]"
            row["note"] += presync_note
            return row, current_subs

        _write_fix(subtitle_path, cfg, media_root, tmp_primary)
        # Kept for _try_anchor_resync: when alass's own fit turns out to be wrong, the anchors can
        # often still plan a correct one -- but only from the UNTOUCHED original, since planning
        # on top of a bad fit inherits its damage. Popped in correctness_and_finish; never
        # persisted (same handling as _ambiguous_sync).
        row["_pre_sync_subs"] = baseline_subs
        row["_orig_subs"] = old_subs  # rate step measures the untouched file
        row["sync_status"] = f"fixed (Δ{max_shift:.1f}s)"
        row["note"] += presync_note + blocks_note + structural_note
    return row, current_subs


# Tie-break order between sync candidates in _resolve_ambiguous_sync when the evidence can't
# separate them: alass's single-offset fit (can't overfit in pieces) over its multi-block fit
# (alass's evidence that a shift is needed, but structurally the riskier one) over leaving the
# original alone (alass measured a real offset, so "untouched" is not the neutral choice).
_CANDIDATE_PREFERENCE = {"new": 0, "blocks": 1, "old": 2}

# A lone anchor this far out is a different phenomenon than the jitter the
# min_samples rule was measured against (1-2 samples at 2.5-5s on 5 of 52 real
# episodes): at 4x the noise floor and content-matched, it is a whole block off,
# witnessed once because sparse sampling only landed one clip there -- a wrong
# region gets NO anchor at all, never a small one. Only trusted on the
# block-error shape (multi-block spread), where it routes to the anchor branch:
# resync either fixes-verified or the file warns. Never a verdict on its own.
# Measured: SH_S01E01 sampled keeps a 4-block fit 25s off in one range,
# witnessed by a lone 25.27s anchor (rec 0.855, was silent).
ANCHOR_HUGE_SINGLE_S = 10.0

# How close two "ok" candidates' CONTENT scores (avg_score -- fraction of matching words) have
# to be before timing/preference gets to break the tie, once content has actually been scored
# for more than one candidate. A real gap this size or bigger is a genuine "one of these
# matches the episode's actual dialogue much better" signal that anchors must not overrule --
# verified against a real file where the single-offset fit scored 0.43 against the multi-block
# fit's 0.90 (more than half its own sampled text failed to match anything) and STILL won,
# because the old code picked among "ok" candidates using the same anchor-only pick() the free
# fast-path gate uses, silently discarding the content scores it had just paid API calls for.
CONTENT_SCORE_TIE_MARGIN = 0.1


# Ramp rescue only: keep 0.85 when the line is overwhelmingly linear and flat -- block
# pools reach rho <= 0.95 at gain <= 6.7. Calibrated on 2 configs (C_S02E04); revert
# this path if it ever moves a non-drift cell.
RAMP_RESCUE_KEEP_FRAC = 0.85
RAMP_RESCUE_RHO_MIN = 0.99
RAMP_RESCUE_GAIN_MIN_S = 10.0


def _ramp_overwhelming(p: Optional[dict]) -> bool:
    """The stretch gates with keep eased to 0.85, for an overwhelming line only."""
    if p is None or p.get("rho") is None:
        return False
    return (p["n"] >= STRETCH_MIN_POINTS and abs(p["tilt"]) >= STRETCH_MIN_TILT_S
            and abs(p["slope"]) <= STRETCH_MAX_RATE and abs(p["rho"]) >= RAMP_RESCUE_RHO_MIN
            and p["gain"] >= RAMP_RESCUE_GAIN_MIN_S and p["resid"] <= STRETCH_MAX_RESID_S
            and p["keep_frac"] >= RAMP_RESCUE_KEEP_FRAC)


def _ramp_rescue_probe(result: dict, new_subs, cfg: Config) -> Optional[dict]:
    """The stretch probe when 'new's full pool is one clean rate line, else None.

    alass staircases a drifted file and the staircase wins the comparison (it fits
    better locally); the ramp survives only in the full pool. Checks the same guards
    as the downstream fix, so a rescue always lands in a write. No Whisper."""
    if not result.get("full_coverage") or not cfg.fps_check_enabled:
        return None
    pts = _fps_points(result)
    probe = _dense_probe(pts)
    if not _stretch_gates_pass(probe) and not _ramp_overwhelming(probe):
        return None
    ratio = stretch_ratio(probe)
    if max_quartile_residual_after(pts, ratio, probe["intercept"]) > FPS_RESID_MAX_S:
        return None
    trial = copy.deepcopy(new_subs)
    if apply_fps_rescale(trial, ratio, offset=probe["intercept"]) < cfg.min_change_seconds:
        return None
    return probe


def _resolve_ambiguous_sync(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path,
                             lang: Optional[str], cfg: Config, media_root: Path,
                             ambiguous: dict, result: dict, row: dict, cancel_event=None) -> tuple:
    """Decides which of {alass's single-offset fit ("new", the candidate the correctness
    check `result` was run on), its multi-block split-penalty fit ("blocks", absent for a
    single-block deferral), the pre-alass BASELINE ("old", presynced when presync fired)} to
    actually keep -- see sync_pair for when this arises. Every candidate is judged on the SAME
    cached Whisper samples (correctness.evaluate_against_cached_transcripts -- the ones the
    correctness check just paid for, plus any older ones for this video), so no new Whisper
    calls, and no candidate gets an easier or harder sample set than another.

    Two questions, answered by two different kinds of evidence -- keeping them apart is the
    whole point:

    1. TIMING -- which candidate's cues actually sit where the speech is? Only the per-line
       anchors (subtitles.clip_anchor_shift) can answer this: the window-overlap score compares
       +/-window_minutes of text around each clip and is therefore blind to any shift smaller
       than that window (measured: identical scores for a 0.5s and a 25s offset, and a 51s
       offset still passed the majority vote). A challenger only wins on timing if its mean
       |residual| on the clips BOTH it and the default have a confident anchor in is smaller by
       more than subtitles.ANCHOR_PREFER_MARGIN_S (anchor noise floor); otherwise the default
       order _CANDIDATE_PREFERENCE stands.

       The default (alass's single-offset fit) additionally has to POSITIVELY confirm itself
       (_confirmed_in_every_block) before it gets to skip content-scoring the alternatives --
       "no rival clearly beat it" is not the same claim as "it's actually right everywhere",
       and with only a handful of samples spread across a whole episode, a multi-block file can
       easily have a block where NEITHER candidate has any usable anchor at all, leaving the
       comparison silently blind to it. Verified against a real 21-minute episode where alass
       correctly split-fit two ~10-11 minute blocks: only 2 of 3 sampled clips produced any
       anchor, one per block, and the single-offset fit's own anchor in the first block already
       showed a real (if small) residual -- clearly_better let it through anyway because
       nothing else in that block had an anchor to outscore it with, even though the
       single-offset fit was actually wrong across nearly all ten minutes of that block. No
       anchors at all (subtitle in another language than the audio, no cached segments) = no
       timing evidence = trust alass's single-offset fit, exactly what every fix did before
       this comparison existed.
    2. CONTENT -- is this even the right episode's text? The ordinary majority-vote overlap
       score. Costs LLM translation calls for a foreign-language subtitle, so it is only
       computed for the alternatives when step 1 didn't already confirm the default with a
       content-"ok" result of its own. Candidates that fail it are only ever kept when NONE
       passes (best score wins, and the caller flags the file SUSPECT either way).

    Only called when cfg.dry_run is False (see sync_pair) -- no dry-run branching needed here.

    Returns (current_subs, result, swap_severity, winner) — result/swap_severity are shaped
    like finalize_line_order's own return so the rest of correctness_and_finish can keep
    treating them the same regardless of which candidate won. old/blocks get a SYNTHESIZED
    result (the cached-sample evaluation, anchors included, so the caller's anchor escalation
    applies to them just like to "new") with no line-order data (swap_severity=None, no
    line_issues/line_flagged) — that's a separate, heavier analysis pass this cheap comparison
    doesn't redo; it'll run properly next time this file's content actually differs from what's
    cached."""
    old_subs, new_subs = ambiguous["old_subs"], ambiguous["new_subs"]
    blocks_subs = ambiguous.get("blocks_subs")
    orig_subs = ambiguous["orig_subs"]
    had_presync = bool(ambiguous.get("had_presync"))
    single_block = bool(ambiguous.get("single_block"))
    transcript_lang = result.get("audio_lang")
    subs_by_key = {"new": new_subs, "blocks": blocks_subs, "old": old_subs}
    subs_by_key = {k: v for k, v in subs_by_key.items() if v is not None}

    # whisper_mode == "full" -- collect_samples_full has already populated the full-track cache
    # earlier in this same run, so this is still free (no new Whisper calls) and gives dense,
    # whole-file evidence instead of a handful of cached clips.
    _evaluate = evaluate_against_full_transcript if cfg.whisper_mode == "full" else evaluate_against_cached_transcripts

    # 1. Timing evidence -- free.
    timing: dict[str, Optional[dict]] = {}
    for key, subs in subs_by_key.items():
        ev = _evaluate(conn, video_path, subs, lang, transcript_lang, cfg, score=False)
        timing[key] = summarize_anchor_samples(ev["samples"]) if ev else None

    def residuals_on_common(a: str, b: str) -> Optional[tuple[float, float]]:
        ta, tb = timing.get(a), timing.get(b)
        if not ta or not tb:
            return None
        common = set(ta["regions"]) & set(tb["regions"])
        if not common:
            return None
        return (sum(abs(ta["regions"][s]) for s in common) / len(common),
                sum(abs(tb["regions"][s]) for s in common) / len(common))

    def clearly_better(a: str, b: str) -> bool:
        pair = residuals_on_common(a, b)
        return pair is not None and pair[0] + ANCHOR_PREFER_MARGIN_S < pair[1]

    def pick(pool) -> str:
        ranked = sorted(pool, key=lambda k: _CANDIDATE_PREFERENCE[k])
        best = ranked[0]
        for k in ranked[1:]:
            if clearly_better(k, best):
                best = k
        return best

    def _confirmed_in_every_block(key: str) -> bool:
        """True only when `key` has a confident anchor, with residual inside the noise margin,
        somewhere in EVERY block alass's multi-block fit found -- the bar for trusting a
        candidate's OWN timing enough to skip content-scoring the alternatives entirely (the
        cheap fast path below). "Not proven worse than another candidate on whatever few clips
        happened to land" (clearly_better/pick above) is a real but WEAKER claim than "proven
        right, here, in every block" -- measured on a real file where alass split-fit two
        blocks (~10min / ~11min) and only 2 of 3 sampled clips produced any anchor at all, one
        per block; the single-offset fit's own anchor in the SECOND block was fine, but its
        anchor in the FIRST was already 4.3s off (a real residual, just under the file's own
        1s-margin comparison to an even-worse rival) -- clearly_better alone let it through
        because nothing else in that block had a usable anchor to outscore it with, even though
        the single-offset fit was actually wrong across nearly all of that block's ten minutes.
        A candidate that lacks anchor evidence in some block, or whose anchor there shows a
        real residual, has not actually demonstrated it gets that block right. Only meaningful
        when anchors are structurally possible at all (see anchors_applicable) -- a subtitle in
        another language than the audio has NO anchors for any candidate, which must stay
        "no timing evidence, trust the default" (see the caller), never "nothing confirmed it,
        so distrust the default": the latter would force every foreign-language ambiguous file
        into paying for content-scoring/translation of every candidate, for nothing -- anchors
        were never going to be available to confirm ANY candidate there."""
        block_ranges = ambiguous.get("blocks_time_ranges") or []
        if not block_ranges and ambiguous.get("single_block"):
            block_ranges = [(0.0, float("inf"))]
        t = timing.get(key)
        if not block_ranges or not t:
            return False
        for lo, hi in block_ranges:
            if not any(lo <= pos < hi and abs(shift) <= ANCHOR_PREFER_MARGIN_S
                       for pos, shift in t["regions"].items()):
                return False
        return True

    # Anchors structurally exist at all for this subtitle only if at least one candidate has
    # SOME anchored clip -- if none do (wrong language, or no cached segments for any sample),
    # _confirmed_in_every_block can never be satisfied by ANY candidate, and requiring it would
    # just force content-scoring every time for no reason. In that case fall back to the plain
    # "nothing disproved the default" gate, same as before per-block confirmation existed.
    any_anchor_evidence = any(timing.get(k) for k in subs_by_key)

    def _old_wins_fairly() -> bool:
        """'old' (leave the file completely untouched) may only win when either no timing
        evidence exists at all (nothing to contradict it) or it clears the SAME per-block bar
        'new' needs to win the free fast path (_confirmed_in_every_block). 'old' is the
        highest-stakes of the three possible winners: it means a file alass itself measured a
        real, substantial multi-block spread on gets reported "already in sync", and nothing
        checks it again until its content changes. Without this guard, 'old' can win on 1-2
        lucky clips landing in whichever part of the file already happened to be fine --
        reproduced on a real 21-minute episode with a genuine 5-block, ~43s-spread drift: 'old'
        had confident anchors at only 2 clips, both inside its one unbroken stretch, and could
        never show a BAD anchor in the ~9 genuinely broken minutes, because a wrong candidate
        doesn't get a bad anchor in a region it's wrong about -- it gets NO anchor there at all
        (see _confirmed_in_every_block's own docstring) -- so those minutes contributed zero
        evidence against it, and it won a comparison that never actually looked at them."""
        # No single-block exception: measured 10 new silent rows (fix2 ablation).
        return (not any_anchor_evidence) or _confirmed_in_every_block("old")

    def _reject_unproven_old(winner: str, pool) -> str:
        """Applied after every winner determination in the content-evidence branch below:
        downgrades an 'old' win that fails _old_wins_fairly() to whichever OTHER candidate in
        `pool` pick() itself would have chosen -- NOT simply the best content score among the
        rivals. Those are different questions: pick() already correctly weighed anchor timing
        evidence between 'new' and 'blocks' before 'old' ever entered the comparison (in the
        same real case that motivated this function, pick() had already correctly ranked 'new'
        over 'blocks' on their own anchor residuals -- 7.1s vs 12.5s -- before 'old' briefly,
        wrongly, beat 'new' on 2 unrepresentative lucky clips and got rejected here). Re-ranking
        by content score ALONE at this point would silently throw that timing comparison away
        and could resurrect the very candidate pick() had already correctly rejected -- which is
        exactly what a first version of this function did, picking 'blocks' (a worse anchor
        residual, marginally higher content score within the same noise-level tie margin the
        caller already decided not to trust on its own) instead of 'new'. Falls back to 'new'
        -- the safe, single-offset default -- only if the pool has nothing left to rank."""
        if winner != "old" or _old_wins_fairly():
            return winner
        rivals = [k for k in pool if k != "old"]
        return pick(rivals) if rivals else ("new" if "new" in subs_by_key else winner)

    # 2. Content evidence -- only when timing alone didn't confirm the default.
    scored: dict[str, dict] = {"new": {"avg_score": result.get("avg_score"), "flag": result.get("flag"),
                                       "samples": result.get("samples") or []}}
    content_matched = True
    # A clean rate line in the full pool outranks the comparison. Text must still
    # verify (on new, else on old -- a cut file's fake ramp dies there), and a
    # baseline that already verifies (0.2-0.3s; drift sits 12-30s out) is not rescued.
    old_t = timing.get("old")
    old_verifies = (old_t is not None and old_t["mean_abs_shift"] <= ANCHOR_PREFER_MARGIN_S
                    and len(old_t["regions"]) >= SCREEN_MIN_CLIPS)
    ramp_probe = None if old_verifies else _ramp_rescue_probe(result, new_subs, cfg)
    ramp_via_old = False
    ramp_decided = False
    old_scored = False
    new_confirmed = _confirmed_in_every_block("new") if any_anchor_evidence else True
    if ramp_probe is not None and result.get("flag") == "ok":
        winner, ramp_decided = "new", True
    elif result.get("flag") == "ok" and pick(subs_by_key) == "new" and new_confirmed:
        winner = "new"
    else:
        if ramp_probe is not None and result.get("flag") != "ok":
            ev_old = _evaluate(conn, video_path, old_subs, lang, transcript_lang, cfg,
                               score=True, cancel_event=cancel_event)
            old_scored = True
            if ev_old is not None:
                scored["old"] = ev_old
            if ev_old is not None and ev_old.get("flag") == "ok" and ev_old.get("avg_score") is not None:
                winner, ramp_via_old, ramp_decided = "new", True, True
        if not ramp_via_old:
            for key, subs in subs_by_key.items():
                if key == "new" or (key == "old" and old_scored):
                    continue
                ev = _evaluate(conn, video_path, subs, lang, transcript_lang, cfg,
                               score=True, cancel_event=cancel_event)
                if ev is not None:
                    scored[key] = ev
            content_ok = [k for k, v in scored.items() if v.get("flag") == "ok" and v.get("avg_score") is not None]
            if not content_ok:
                # NOTHING matched the audio's content -- not alass's fit(s), not the baseline. That
                # is a wrong-subtitle verdict, not a sync problem, and re-timing text that isn't this
                # episode's is meaningless. Leave the file exactly as it is and let the caller flag it
                # SUSPECT: picking a "best" fit here overwrote the original on the strength of scores
                # like 0.07 -- see C_S02E15, whose subtitle turned out to be the NEXT episode's text
                # entirely, and which the pipeline "fixed" by 30.4s before this branch existed.
                winner, content_matched = "old", False
            else:
                # CONTENT decides first among candidates that passed it -- it's the evidence this
                # branch just paid for, and a real gap between two "ok" scores is exactly what it
                # exists to catch (see CONTENT_SCORE_TIE_MARGIN). Anchors/preference (pick()) only
                # get a say among whichever candidates are within that margin of the best score --
                # a genuine tie, not a decisive difference silently thrown away.
                best = max(scored[k]["avg_score"] for k in content_ok)
                near_best = [k for k in content_ok if scored[k]["avg_score"] >= best - CONTENT_SCORE_TIE_MARGIN]
                winner = _reject_unproven_old(pick(near_best), near_best)

    # Veto a timing-disproven winner: >= 3 own anchors > 2.5 s out while old is
    # clearly better on the same clips AND on average. Never overrules a ramp
    # rescue, nor a decisive content gap (anchors don't outvote clearly better
    # text matching -- see CONTENT_SCORE_TIE_MARGIN above).
    vetoed_from = None
    if winner in ("new", "blocks") and not ramp_decided:
        wt, ot = timing.get(winner), timing.get("old")
        ws = (scored.get(winner) or {}).get("avg_score")
        os_ = (scored.get("old") or {}).get("avg_score")
        content_tie = (ws is not None and os_ is not None
                       and abs(ws - os_) <= CONTENT_SCORE_TIE_MARGIN)
        if wt is not None and ot is not None and content_tie:
            bad = sum(1 for s in wt["regions"].values()
                      if abs(s) > ANCHOR_SUSPECT_THRESHOLD_S)
            if (bad >= 3 and clearly_better("old", winner)
                    and ot["mean_abs_shift"] <= wt["mean_abs_shift"]):
                vetoed_from, winner = winner, "old"
                row["_vetoed_bad_fit"] = True

    def _describe(key: str) -> str:
        parts = []
        s = scored.get(key) or {}
        if s.get("avg_score") is not None:
            parts.append(f"score {s['avg_score']:.2f} ({s.get('flag')})")
        t = timing.get(key)
        if t:
            parts.append(f"anchor residual {t['mean_abs_shift']:.1f}s over {len(t['regions'])} clip(s)")
        return f"{key}: " + (", ".join(parts) if parts else "no evidence")

    n_blocks = ambiguous.get("blocks_split_count") or "?"
    base_name = "presynced baseline" if had_presync else "original"
    if single_block:
        note_suffix = (f" Verified alass's single-offset fit against the {base_name} "
                       f"on the same Whisper samples before applying anything — kept '{winner}' ["
                       + "; ".join(_describe(k) for k in subs_by_key) + "].")
    else:
        note_suffix = (f" Verified alass's {n_blocks}-block fit against its single-offset fit and the {base_name} "
                       f"on the same Whisper samples before applying anything — kept '{winner}' ["
                       + "; ".join(_describe(k) for k in subs_by_key) + "].")
    if ramp_decided:
        via = " (text verified on the original; new's own content misses its windows)" if ramp_via_old else ""
        note_suffix += (f" Ramp rescue: the full anchor pool shows one clean rate line "
                        f"(tilt {ramp_probe['tilt']:+.1f}s over {ramp_probe['n']} anchors, "
                        f"rho {ramp_probe['rho']:+.2f}, {ramp_probe['keep_frac']:.0%} on the line), "
                        f"so the single-offset fit was kept for the rate fix below{via}.")
        row["_ramp_rescued"] = ramp_probe
    if vetoed_from is not None:
        note_suffix += (f" Veto: '{vetoed_from}' disproven by its own anchors "
                        f"({bad} clips > {ANCHOR_SUSPECT_THRESHOLD_S:g}s out) while the "
                        f"{base_name} measures clearly better -- kept the original.")
    structural_note = " line count changed significantly — check the file manually." if ambiguous.get("structural") else ""

    def _synthetic(key: str) -> dict:
        # Falls back to "new"'s own evaluation for a candidate that was never scored -- the
        # no-content-match branch above can pick 'old' without having anything cached to score it
        # against, and the caller still has to see a real (failing) flag rather than None.
        s = scored.get(key) or scored["new"]
        return {"avg_score": s.get("avg_score"), "flag": s.get("flag"), "samples": s.get("samples") or [],
                "audio_lang": transcript_lang, "swap_severity": None}

    max_shift_new = ambiguous.get("max_shift_new")
    winner_shift = {"new": max_shift_new, "blocks": ambiguous.get("max_shift_blocks")}.get(winner)
    if _below_min_change(cfg, winner_shift):
        # The comparison ran and picked a fit, but it moves less than min_change_seconds. Nothing
        # is on disk yet at this point, so declining the write leaves the original untouched.
        row["sync_status"] = "already in sync"
        row["sync_max_shift_s"] = round(winner_shift, 2)
        row["sync_split_blocks"] = None
        row["sync_block_spread_s"] = None
        row["note"] += structural_note + note_suffix + (
            f" Not written: Δ{abs(winner_shift):.2f}s is under the "
            f"{cfg.min_change_seconds}s threshold.")
        if had_presync:
            # Disk holds the original; baseline never left temp. Winner is within the
            # threshold of the original, so its verdict proxies the original's.
            w = _synthetic(winner)
            return orig_subs, w, w.get("swap_severity"), "old"
        # _synthetic("old"), not `result`: what stays on disk is the original, so the verdict
        # handed back has to be the original's. The two are close here by construction (the
        # move was under the threshold), which is exactly why the mismatch could sit unnoticed.
        return old_subs, _synthetic("old"), _synthetic("old").get("swap_severity"), "old"

    if winner == "new":
        _write_fix(subtitle_path, cfg, media_root, new_subs)
        row["sync_status"] = f"fixed (Δ{max_shift_new:.1f}s)" if max_shift_new is not None else "fixed"
        row["sync_max_shift_s"] = round(max_shift_new, 2) if max_shift_new is not None else None
        row["sync_split_blocks"] = 1
        row["sync_block_spread_s"] = None
        row["note"] += structural_note + note_suffix
        return new_subs, result, result.get("swap_severity"), winner

    if winner == "blocks":
        max_shift_blocks = ambiguous.get("max_shift_blocks")
        _write_fix(subtitle_path, cfg, media_root, blocks_subs)
        row["sync_max_shift_s"] = round(max_shift_blocks, 2) if max_shift_blocks is not None else None
        row["sync_split_blocks"] = ambiguous.get("blocks_split_count")
        row["sync_block_spread_s"] = ambiguous.get("blocks_spread")
        shift_txt = f"{max_shift_blocks:.1f}s" if max_shift_blocks is not None else "?"
        row["sync_status"] = f"fixed (Δ{shift_txt}, {n_blocks} sync block(s))"
        row["note"] += structural_note + note_suffix
        row["line_order_fixed"] = None
        row["line_order_flagged"] = None
        return blocks_subs, _synthetic("blocks"), None, winner

    if had_presync and content_matched:
        # Baseline won and it never reached disk: presync only ever touched temp.
        _write_fix(subtitle_path, cfg, media_root, old_subs)
        presync_shift, *_ = max_shift_stats(orig_subs, old_subs)
        shift_txt = f"{presync_shift:.1f}s" if presync_shift is not None else "?"
        row["sync_status"] = f"fixed (Δ{shift_txt}, presync)"
        row["sync_max_shift_s"] = round(presync_shift, 2) if presync_shift is not None else None
        row["sync_split_blocks"] = 1
        row["sync_block_spread_s"] = None
        row["note"] += note_suffix
        row["line_order_fixed"] = None
        row["line_order_flagged"] = None
        return old_subs, _synthetic("old"), None, winner
    if had_presync and not content_matched:
        # Wrong text: re-timing it is meaningless, disk stays the original.
        shift_txt = f"{max_shift_new:.1f}s" if max_shift_new is not None else "?"
        row["sync_status"] = (
            f"left unchanged (alass suggested Δ{shift_txt}; no candidate matched the audio's content)")
        row["sync_split_blocks"] = None
        row["sync_block_spread_s"] = None
        row["note"] += note_suffix
        row["line_order_fixed"] = None
        row["line_order_flagged"] = None
        return orig_subs, _synthetic("old"), None, winner
    # winner == "old" -- nothing to write, the file was never touched on disk in the first place.
    # sync_max_shift_s deliberately keeps the offset alass measured: it's the one fact worth
    # seeing in the UI about a file whose re-sync was rejected.
    shift_txt = f"{max_shift_new:.1f}s" if max_shift_new is not None else "?"
    row["sync_status"] = (
        f"left unchanged (alass suggested Δ{shift_txt}; no candidate matched the audio's content)"
        if not content_matched else
        f"already in sync (alass suggested Δ{shift_txt}, rejected — didn't verify better than the original)")
    row["sync_split_blocks"] = None
    row["sync_block_spread_s"] = None
    row["note"] += note_suffix
    row["line_order_fixed"] = None
    row["line_order_flagged"] = None
    return old_subs, _synthetic("old"), None, winner



def _try_anchor_resync(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path,
                        lang: Optional[str], cfg: Config, media_root: Path, subs,
                        transcript_lang: Optional[str], fallback_subs=None,
                        cancel_event=None) -> Optional[tuple]:
    """Corrects a file the anchors have just proved mis-timed, instead of only flagging it.

    An anchor is already a MEASURED offset at a known instant (subtitles.clip_anchor_shift), so a
    run of agreeing anchors is the correction for that stretch -- the information to fix these
    files was always there, it just wasn't being used: every one of them was reported SUSPECT with
    the right number sitting in its own note. Handles the multi-block case too (a real one:
    -3.3s, then -17.5s, then 0, then -23.3s, then 0 across five stretches of one episode), because
    a single global offset cannot fix a file like that and picking the average would make every
    region wrong.

    Only ever called from the anchor-escalation branch, i.e. only for a file whose CONTENT already
    checked out -- re-timing a subtitle that isn't this episode's text is the mistake
    _resolve_ambiguous_sync's no-content-match branch exists to prevent, and this must not
    reintroduce it from the other side.

    Nothing is written until the correction has been re-measured against the same evidence
    (see below): the plan is applied to a COPY, fresh anchors are computed for it, and the file on
    disk is only replaced if those anchors actually came back clean. A correction that doesn't
    verify is discarded and the caller falls back to flagging SUSPECT -- a wrong auto-correction
    is worse than an unfixed file, since nothing downstream would ever look at it again.

    Returns (new_subs, note, plan) or None to fall back."""
    evaluate = evaluate_against_full_transcript if cfg.whisper_mode == "full" else evaluate_against_cached_transcripts

    def anchors_for(candidate):
        kwargs = {"anchor_interval": ANCHOR_RESYNC_INTERVAL_S} if cfg.whisper_mode == "full" else {}
        ev = evaluate(conn, video_path, candidate, lang, transcript_lang, cfg, score=False, **kwargs)
        return (ev or {}).get("samples") or []

    # alass runs before any of this, so `subs` may already carry a WRONG fit -- and a bad fit is
    # harder to plan from than the untouched file (measured: 4 of 9 real mis-synced files could
    # not be planned after alass had moved them, 2 of those plan cleanly from the original).
    # Both are tried; the validation below is what decides, so a worse candidate simply fails.
    # Both candidates are planned and verified, and the BEST verified one wins -- not the first.
    # Measured against an independent reference: planning on top of alass's fit passes
    # verification but lands further out than planning from the untouched file (C_S03E05 worst
    # twelfth 14.8s vs 1.0s, C_S03E20 6.5s vs 1.3s). Ties keep alass's fit, the conservative side.
    best = None
    for candidate in [c for c in (subs, fallback_subs) if c is not None]:
        plan = plan_anchor_resync(candidate, anchors_for(candidate))
        if not plan:
            continue
        fixed = apply_anchor_resync(candidate, plan)
        residual = _resync_verified(plan, anchors_for(fixed), cfg, subtitle_path)
        if residual is not None and (best is None or residual < best[0]):
            best = (residual, fixed, plan)
    if best is None:
        return None
    _residual, fixed, plan = best
    _write_fix(subtitle_path, cfg, media_root, fixed)
    shifts = ", ".join(f"{r['shift']:+.1f}s x{r['n']}" for r in plan)
    note = (f" Re-synced from {sum(r['n'] for r in plan)} Whisper anchor(s) in "
            f"{len(plan)} region(s) [{shifts}] and re-verified against the audio afterwards.")
    return fixed, note, plan


def _resync_verified(plan, after, cfg: Config, subtitle_path: Path) -> Optional[float]:
    """The corrected file's mean remaining |residual| if its own fresh anchors come back clean,
    else None. A number rather than a bool so two candidates can be compared, not just accepted."""
    # An anchor whose own window straddles a region cut covers two different offsets at once, so
    # its median sits between them and means nothing -- the same reason _judge_order discounts a
    # word heard on both sides of its split. Judging the correction on those would reject a
    # perfectly good multi-region fix for the one thing it cannot get right by construction.
    cuts = [r["cut_audio_s"] for r in plan if r.get("cut_audio_s") is not None]
    span = float(cfg.clip_seconds)
    # Samples with no confident anchor carry no timing evidence either way (normal and
    # common -- a silent/music stretch simply anchors nothing), so they are excluded up
    # front like the cut-straddlers: without this the mean below crashes on None.
    clean = [s for s in after
             if s.get("anchor") is not None
             and not any(s["start"] - span <= c <= s["start"] + span for c in cuts)]
    # The correction has to hold up on the SAME measurement that condemned the file, densely
    # sampled -- not merely "better on average", which a half-right fix also manages.
    still_bad = significant_anchor_residuals(clean, ANCHOR_SUSPECT_THRESHOLD_S)
    if still_bad or not clean:
        log.info("Anchor resync for %s discarded: %d region(s) planned, but %d of %d anchor(s) "
                 "still mismatched afterwards", subtitle_path.name, len(plan), len(still_bad), len(clean))
        return None
    # Clean anchors are not enough: a region we shifted but cannot re-measure is an UNVERIFIED
    # correction, not neutral evidence. A region still wrong afterwards doesn't produce a bad
    # anchor there -- it produces none at all (same blind spot _confirmed_in_every_block exists
    # for), so "every surviving anchor is clean" passes on a half-right fix. Measured: 28 of 30
    # silent block rows said "fixed" at rec 0.42-0.90, i.e. up to 272 of 486 cues still >1s out.
    unverified = _regions_without_anchor(plan, clean, span)
    if unverified:
        log.info("Anchor resync for %s discarded: %d of %d planned region(s) had no anchor to "
                 "verify the correction against", subtitle_path.name, len(unverified), len(plan))
        return None
    return sum(abs(s["anchor"]["shift"]) for s in clean) / len(clean)


def _regions_without_anchor(plan, clean, span: float) -> list[int]:
    """Indices of planned regions wide enough to hold a non-straddling anchor that still ended
    up with none -- the regions whose correction nothing measured. Narrow regions are exempt:
    between two cuts less than 2*span apart every anchor straddles one by construction, and
    rejecting on those would throw away good multi-region fixes (see the cut filter above)."""
    edges = [r["cut_audio_s"] for r in plan if r.get("cut_audio_s") is not None]
    out = []
    for i in range(len(plan)):
        lo = edges[i - 1] if i > 0 else float("-inf")
        hi = edges[i] if i < len(edges) else float("inf")
        if hi - lo <= 2 * span:
            continue
        if not any(lo <= s["start"] < hi for s in clean):
            out.append(i)
    return out





def _fps_points(result: dict) -> list:
    """Raw (audio, subtitle) anchor points pooled over every collected clip, confident
    or not -- the evidence pool for the framerate tilt fit. Prefers the pooled
    collected["fps_points"] (complete by construction); falls back to per-sample
    anchor_points for results that predate the pool (ambiguous-winner synthetics
    built from evaluate_* samples, legacy cache rows)."""
    pts = list(result.get("fps_points") or [])
    if not pts:
        for s in result.get("samples") or []:
            pts.extend(s.get("anchor_points") or [])
    return [(float(a), float(b)) for a, b in pts]


def _anchor_signature_passes(sig: Optional[dict]) -> Optional[float]:
    """The anchor side of the framerate decision: plain tilt, binned tilt and every
    leave-one-eighth-out tilt clear their floors with the SAME sign (see
    subtitles.anchor_drift_signature for why all three). Returns the signed plain
    tilt on pass, None on veto. Pure policy over a measurement -- shared by the
    sampled trigger (_fps_says_needs_full) and the fix below."""
    if sig is None:
        return None
    tilt, binned, drops = sig["tilt"], sig["binned"], sig["drops"]
    if tilt is None or binned is None or any(d is None for d in drops):
        return None
    if abs(tilt) < FPS_ANCHOR_TILT_MIN_S or abs(binned) < FPS_BINNED_TILT_MIN_S:
        return None
    if any((d > 0) != (tilt > 0) or abs(d) < FPS_LOO_TILT_MIN_S for d in drops):
        return None
    if (binned > 0) != (tilt > 0):
        return None
    return tilt


def _stretch_gates_pass(p: Optional[dict]) -> bool:
    """The five readings that separate a whole-file rate error from a block error. Shared by
    the post-alass fix (_try_stretch_rescale) and the pre-alass correction
    (presync_from_screen) so the two can never disagree about what a stretch looks like."""
    return probe_gates_pass(p, STRETCH_MIN_POINTS, STRETCH_MIN_TILT_S, STRETCH_RHO_MIN,
                            STRETCH_MIN_KEEP_FRAC, STRETCH_MIN_GAIN_S, STRETCH_MAX_RESID_S)


def _try_stretch_rescale(subtitle_path: Path, cfg: Config, media_root: Path,
                         current_subs, pts: list):
    """Fixes a stretched subtitle by the MEASURED rate, for the case the discrete
    1001/1000 ratios cannot reach: a whole-file rate error big enough to walk the end
    of the file 8+ seconds out (a 2% stretch moves it 25s on 21 minutes). The discrete
    path cannot see it at all -- its +-8s median trim discards a +-25s signal.

    The hard part is not seeing the tilt; a block error tilts too, often further. It is
    telling a rate error from a block error, and five readings have to agree
    (subtitles.stretch_probe, thresholds and their evidence in subtitles.STRETCH_*):

      keep_frac  one straight line explains almost the whole anchor pool. THE
                 discriminator: a stretch is one line end to end, while k blocks
                 leave a line reaching about 1/k of the anchors.
      gain       removing the fitted ramp collapses the residual spread...
      resid      ...to where a healthy file sits
      |rho|      the offset is ordered, not stepped
      |rate|     above PAL+NTSC compounded is a broken pool, not a rate

    Monotonicity alone is NOT enough, which is worth saying because it looks like it
    should be: 5 of 420 block-error pools reach |rho| 0.91-0.95 with gains up to +6.7s
    and residuals as low as 0.13s. keep_frac is what stops all five. Ablation over 914
    pools: dropping keep_frac costs 6 false positives, the rate cap 8, resid 1; tilt,
    rho and the point floor cost none there and are kept only as redundancy.

    Measured through the matrix (tiny.en, 280 rows): 14 rows corrected, 0 false
    positives. drift recovery 0.061 -> 0.164 sampled and 0.093 -> 0.201 full;
    piecewise, clean, uniform and gap unchanged. Separately over 18 episodes x 9
    scenarios: 7 of 36 stretched episodes corrected, 0 of 180 others touched.

    The catch rate is low for one reason, and it is not the gates: alass runs BEFORE
    this and rewrites the file, so what reaches here is no longer the original error.
    On a 2% stretch alass guesses a false PAL conversion and applies it on top,
    turning 2% into 6.25% (measured, C_S03E03), and of 144 stretch runs 32 left no
    line through even 8 anchors. Measured on the same files BEFORE alass the pool
    reads the true rate (-1.99%, keep_frac 0.93, rho -0.99) and a correction from it
    lands 12 of 12 files inside 1s -- so the right home for this is ahead of alass,
    not behind it. See the findings log, sections 10.6-10.8.

    Since presync_from_screen took that job this branch fires 0 times in 4200 matrix
    rows, and no test asserts it fires. It is NOT dead: it is the only stretch fix on
    every path where the screen does not run -- bazarr.py without a `conn`, jobs.py
    when a screen raises, and any file the screen calls "unknown". Keep it, and do
    not read the zero as unused code.

    Unlike the framerate path this passes the fitted intercept to apply_fps_rescale:
    the file is not pivoting at zero any more once alass has shifted it, and undoing
    only the rate leaves that shift behind (measured: 19s on C_S02E01).

    Returns (fixed_subs, note_fragment, info) or None.
    """
    if not cfg.fps_check_enabled:
        return None
    p = stretch_probe([(a, a - s) for a, s in pts])
    if not _stretch_gates_pass(p):
        return None
    return _apply_stretch_fix(subtitle_path, cfg, media_root, current_subs, pts, p)


def _stretch_info(name: str, ratio: float, p: dict, worst: float) -> dict:
    """fps_fix row info for a rate fixed from anchors alone."""
    return {"name": name, "kind": "stretch", "ratio": ratio, "atilt": p["tilt"],
            "vtilt": None, "n_anchors": p["n"], "n_vad": 0, "worst": worst, "probe": p}


def _apply_stretch_fix(subtitle_path: Path, cfg: Config, media_root: Path,
                       current_subs, pts: list, p: dict):
    """Applies a stretch probe that already passed its gates (quartile, threshold, write)."""
    # a = (s + c) / (1 - m): the rate and the offset are one inverse. alass has
    # usually shifted the file already, so undoing only the rate leaves its shift.
    ratio = stretch_ratio(p)
    left = max_quartile_residual_after(pts, ratio, p["intercept"])
    if left > FPS_RESID_MAX_S:
        log.info("stretch rescale %s for %s discarded: %.1fs left in one quarter",
                 f"{(ratio - 1) * 100:+.2f}%", subtitle_path.name, left)
        return None
    fixed, worst = _rescaled(current_subs, ratio, p["intercept"])
    if worst < cfg.min_change_seconds:
        return None
    _write_fix(subtitle_path, cfg, media_root, fixed)
    name = stretch_name(ratio)
    info = _stretch_info(name, ratio, p, worst)
    note = (f" rate {name} offset {p['intercept']:+.1f}s "
            f"(tilt {p['tilt']:+.1f}s over {p['n']} anchors, "
            f"rho {p['rho']:+.2f}, spread {p['gain']:+.2f}s tighter to {p['resid']:.2f}s, "
            f"{p['keep_frac']:.0%} of the pool on the line).")
    log.info("stretch rescale %s for %s: %s", name, subtitle_path.name, note.strip())
    return fixed, note, info


def _try_fps_rescale(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path,
                     lang: Optional[str], cfg: Config, media_root: Path,
                     current_subs, result: dict):
    """Fixes a 24fps-subtitle-on-23.976fps-audio mismatch (or the reverse) by the
    discrete 1001/1000 ratio -- but only on full-transcript evidence, and only when
    two estimators agree there is one:

    * anchors: plain, binned and leave-one-eighth-out tilt all clear their floors
      (0.50/0.50/0.40s) with the same sign (_anchor_signature_passes)
    * VAD tilt: Theil-Sen over (cue start, nearest speech-onset delta), |tilt| >=
      0.30s with the SAME SIGN as the anchor tilt

    Measured through the built pipeline on 35 files (OUT transcripts, production
    sampling): 4/4 real drifting files fixed on full-transcript evidence (sampled
    runs escalate to it via the trigger below), 0/31 others touched.
    Sampled (16-clip) evidence is deliberately NOT enough to fix on: its pools
    are noisy (matching jitter plus 1-2 bad clips per file) and every single
    tilt statistic misfires on some healthy file, so a sampled file that passes
    the anchor gates only TRIGGERS a full-transcript confirmation
    (_fps_says_needs_full) -- the fix below requires full_coverage provenance.
    (An earlier revision fixed straight off sampled evidence and mangled a healthy
    file alass had just repaired: a +0.14s rewrite flipped sparse VAD +0.01s to
    +0.84s, double-fixing +1.4s onto it. The round-trip gate caught it.)

    Gates fail closed: content must verify (flag ok), base anchor spread must be
    sane (excludes wrong-episode/block files), at least 20 anchors, the write
    itself must clear min_change_seconds, and the correction must leave every
    quarter quiet (max_quartile_residual_after -- a fixed ratio that still leaves
    a quarter 1.5s+ out never helped).

    The rescale pivots at the file start (c = 0): that is where a framerate mismatch
    pivots, so exact inversion restores the original timing -- cue lead included. A
    median-preserving offset instead bakes the ramp's own median (0.001 * t_median,
    +0.63s on 21 minutes) in as a permanent global shift.

    Returns (fixed_subs, note_fragment, info) or None. Writes through _write_fix
    like every other fix (backup first when enabled); the caller re-gathers evidence
    on the corrected timing via _recheck_after_resync before the verdict chain runs.
    """
    if not cfg.fps_check_enabled or cfg.dry_run:
        return None
    if (result.get("flag") or "ok") != "ok":
        return None  # never retime what content-matching didn't verify
    if cfg.fps_require_full_coverage and not result.get("full_coverage"):
        return None  # sparse clips trigger (below), they never fix
    pts = _fps_points(result)
    # Large stretch first: it fails every gate below by construction (a 2% stretch
    # blows the base-spread gate and the +-8s median trim throws its own signal
    # away), and the discrete ratios are the wrong size for it anyway.
    stretch = _try_stretch_rescale(subtitle_path, cfg, media_root, current_subs, pts)
    if stretch is not None:
        return stretch
    if len(pts) < FPS_MIN_ANCHORS:
        return None
    if _spread([a - s for a, s in pts]) > FPS_MAX_BASE_SPREAD_S:
        return None
    sig = anchor_drift_signature([(a, a - s) for a, s in pts])
    atilt = _anchor_signature_passes(sig)
    if atilt is None:
        return None
    intervals, _whole = vad_timeline.timeline_for_video(conn, video_path, cfg)
    if not intervals:
        return None
    vtilt, nv = vad_tilt_from_intervals(current_subs, intervals)
    if vtilt is None or abs(vtilt) < FPS_VAD_TILT_MIN_S:
        return None
    if (atilt > 0) != (vtilt > 0):
        return None
    ratio, name = FPS_RATIOS[1] if atilt > 0 else FPS_RATIOS[0]
    left = max_quartile_residual_after(pts, ratio)
    if left > FPS_RESID_MAX_S:
        log.info("fps %s for %s discarded: %.1fs left in one quarter",
                 name, subtitle_path.name, left)
        return None
    fixed = copy.deepcopy(current_subs)
    worst = apply_fps_rescale(fixed, ratio)
    if worst < cfg.min_change_seconds:
        log.info("fps %s for %s discarded: largest change %.2fs under threshold",
                 name, subtitle_path.name, worst)
        return None
    _write_fix(subtitle_path, cfg, media_root, fixed)
    info = {"name": name, "ratio": ratio, "atilt": atilt, "vtilt": vtilt,
            "n_anchors": len(pts), "n_vad": nv, "worst": worst}
    note = (f" fps {name} (anchor tilt {atilt:+.2f}s over {len(pts)} anchors, "
            f"VAD tilt {vtilt:+.2f}s over {nv} cues).")
    return fixed, note, info


def _fps_says_needs_full(collected: dict, cfg: Config) -> bool:
    """Whether sampled anchor pools carry a framerate-drift signature worth buying
    the full transcript to confirm. The same anchor gates as the fix
    (_anchor_signature_passes), but no VAD (sparse clip-cache onsets are too
    flippy to confirm with -- measured: a +0.14s rewrite flipped one file's
    sparse VAD tilt +0.01s to +0.84s) and no content flag (finalize hasn't run
    yet; the fix re-checks everything on full evidence anyway).

    Separate from _screen_says_needs_full on purpose: the screen's switches
    (escalate_sampled_to_full off by default, escalate_only_multi_block) govern
    the anchor-RESYNC escalation, a different signal (local residuals) driving a
    different repair (region planning). This trigger answers only to
    fps_check_enabled, and only fires in sampled mode on non-full evidence --
    measured trigger rate on 35 files: 4/4 drifting, 0/31 others (the four
    confusing healthy files each fail one anchor gate: binned vetoes C_S02E13
    and C_S03E16, leave-one-out vetoes C_S03E09 and C_S03E12)."""
    if not cfg.fps_check_enabled or cfg.whisper_mode != "sampled":
        return False
    if collected.get("full_coverage") or collected.get("skipped"):
        return False
    pts = [(float(a), float(b)) for a, b in (collected.get("fps_points") or [])]
    if len(pts) < FPS_MIN_ANCHORS:
        return False
    sig = anchor_drift_signature([(a, a - s) for a, s in pts])
    if cfg.fps_require_full_coverage:
        return _anchor_signature_passes(sig) is not None
    # A lower bar than the fix: this only buys the look, the fix re-checks every gate on
    # the full pool. The fix's gates missed C_S02E04/C_S02E06 (binned -0.88 vs 0.90; one
    # leave-out tilt) -- real 0.1% drift in the English file: VAD tilt -1.06s, while the
    # Danish file the user watched sits at 0.00-0.14s.
    tilt = (sig or {}).get("tilt")
    return tilt is not None and abs(tilt) >= FPS_ANCHOR_TILT_MIN_S


def _jitter_says_needs_full(collected: dict, cfg: Config) -> bool:
    """Noisy sampled anchors buy the full transcript; the jitter verdict needs it."""
    if cfg.whisper_mode != "sampled" or not cfg.escalate_sampled_to_full:
        return False
    if collected.get("full_coverage") or collected.get("skipped"):
        return False
    jit = anchor_jitter(collected.get("samples") or [])
    return jit is not None and jit >= JITTER_ESCALATE_MAD_S


def _min_words(cfg: Config) -> int:
    """Missing-middle word bar for the transcript model in use."""
    return missing_middle_min_words(full_transcript_cache_key(cfg)[1])


def _missing_middle_probe(video_path: Path, subs, cfg: Config, audio_lang: Optional[str],
                          cancel_event=None) -> Optional[dict]:
    """Sampled mode: transcribe only the bare gaps and judge them like the full path."""
    from verifyarr import line_order as _lo
    from verifyarr import correctness as _corr
    duration = get_duration_seconds(video_path)
    min_words = _min_words(cfg)
    best = None
    with tempfile.TemporaryDirectory() as td:
        for g0, g1 in all_gaps(subs, duration, MISSING_MIDDLE_ESCALATE_GAP_S):
            segs, secs, words = [], 0.0, 0
            for start, dur in gap_probe_windows(g0, g1, float(cfg.clip_seconds)):
                res = _lo._extract_and_transcribe(video_path, start, dur, cfg, audio_lang,
                                                  Path(td), cancel_event=cancel_event)
                _corr.whisper_cost.fresh_s += dur
                for sg in (res or {}).get("segments") or []:
                    try:
                        segs.append({"start": start + float(sg["start"]),
                                     "end": start + float(sg["end"]), "text": sg.get("text", "")})
                    except (KeyError, TypeError, ValueError):
                        continue
                secs, words = gap_speech(_drop_repetition_loops(_drop_nonspeech(segs)), g0, g1)
                if clears_missing_middle(secs, words, min_words):
                    break  # bar met, no need to hear the rest
            if clears_missing_middle(secs, words, min_words) \
                    and (best is None or secs > best["speech_s"]):
                best = {"gap_start": g0, "gap_end": g1, "speech_s": secs, "words": words}
    return best


def _cache_json(collected: dict) -> str:
    """Line-order cache row. fps_points and full_coverage too: without them a rerun
    of an unchanged file skipped missing-middle and jitter (both need coverage)."""
    return json.dumps({
        "samples": collected["samples"], "audio_lang": collected["audio_lang"],
        "whisper_verdicts": collected["whisper_verdicts"],
        "tested_items": collected["tested_items"], "candidates": collected["candidates"],
        "heuristic_indices": collected.get("heuristic_indices"),
        "fps_points": collected.get("fps_points") or [],
        "full_coverage": bool(collected.get("full_coverage")),
    })


def _cache_load(text: str) -> dict:
    """Inverse of _cache_json: JSON turned dict keys and tuples into str and lists."""
    collected = json.loads(text)
    collected["whisper_verdicts"] = {int(k): v for k, v in collected["whisper_verdicts"].items()}
    collected["tested_items"] = [tuple(t) for t in collected["tested_items"]]
    collected["candidates"] = [tuple(c) for c in collected["candidates"]]
    return collected


def _block_repair_parts(row: dict) -> int:
    """Parts in the block repair on disk, else 0: from the status ("N sync block(s)" /
    "N anchor region(s)"), or sync_split_blocks when alass wrote blocks directly."""
    status = row.get("sync_status") or ""
    m = re.search(r"(\d+) (?:sync block|anchor region)\(s\)", status)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"fixed \(Δ[^,()]*s\)", status):
        return row.get("sync_split_blocks") or 0
    return 0


# A block repair is never verified line by line: its edges land where the anchors
# put them. SH tiny: 60 of 68 "fixed" block rows kept 1-58 lines 2s+ off (some were
# correct lines moved with the block). Keep the repair, flag it (arm 2: detect).
BLOCK_REPAIR_NOTE = (" Block error repaired in {n} parts -- lines near the block edges "
                     "cannot be verified; fetch a fresh subtitle.")


def _runs_text(runs: list) -> str:
    return ", ".join(f"{_mmss(r['from'])}-{_mmss(r['to'])} ({r['dev']:+.1f}s)"
                     for r in runs[:4])


def _mmss(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


_FULL_SEG_MEMO = BoundedMemo(8)


def _cached_full_segments(conn: sqlite3.Connection, video_path: Path, cfg: Config
                          ) -> Optional[list]:
    """The cached full transcript, filtered like full_transcript_for_check, or None.
    Filtered once per transcript: 4-6 callers per file each re-ran the filters and
    re-logged their warnings."""
    provider, model = full_transcript_cache_key(cfg)
    cached = db.get_full_transcript_cache(conn, video_path, stt_provider=provider, stt_model=model)
    if cached is None or not cached.get("segments"):
        return None
    raw = cached["segments"]
    key = (str(video_path), provider, model, len(raw),
           json.dumps(raw[:2] + raw[-2:], sort_keys=True, default=str))
    if key not in _FULL_SEG_MEMO:
        _FULL_SEG_MEMO.put(key, _drop_repetition_loops(_drop_nonspeech(raw)))
    return _FULL_SEG_MEMO[key]


def _missing_middle_hit(conn: sqlite3.Connection, video_path: Path, subs, cfg: Config) -> Optional[dict]:
    """Gap evidence (mid, head, tail) from the cached full transcript, or None."""
    segments = _cached_full_segments(conn, video_path, cfg)
    if not segments:
        return None
    return missing_middle_evidence(subs, segments,
                                   duration_s=get_duration_seconds(video_path),
                                   min_words=_min_words(cfg))


def _block_runs_hit(conn: sqlite3.Connection, video_path: Path, subs, cfg: Config,
                    proven_block: bool = False) -> list:
    """Point runs on the file on disk against the cached full transcript, or [].

    proven_block: the file already had a block (resync or alass block fit), so a
    short remainder counts -- 3 lines in a row 5s+ out. Not used otherwise: one
    healthy SH file carries such a triple (repeated line, -13.7s)."""
    segments = _cached_full_segments(conn, video_path, cfg)
    if not segments:
        return []
    pts = dense_anchor_points(subs, segments)
    runs = anchor_point_runs(pts)
    if not runs and proven_block:
        runs = anchor_point_runs(pts, k=3, min_dev=5.0, max_mad=1.0)
    if not runs:
        return runs
    # Whisper timestamps drift for a minute at a time on some models (SH_S01E04 22:47,
    # turbo -3.6s on a correct file). The audio's own speech pattern decides, when set up.
    from verifyarr.vad import speech_timeline, shift_fits_speech
    ivs = speech_timeline(video_path, cfg, conn)
    if not ivs:
        return runs
    kept = []
    for r in runs:
        cues = [(e.start / 1000.0, e.end / 1000.0) for e in subs.events
                if r["from"] - 5 <= e.start / 1000.0 <= r["to"] + 5]
        if shift_fits_speech(ivs, cues, r["dev"]):
            kept.append(r)
        else:
            log.info("block run %s-%s (%+.1fs) not confirmed by VAD -- Whisper timing, not the file",
                     _mmss(r["from"]), _mmss(r["to"]), r["dev"])
    return kept


def _dense_pool(conn: sqlite3.Connection, video_path: Path, subs, cfg: Config) -> list:
    """(audio, cue) for every matched line against the cached full transcript."""
    segments = _cached_full_segments(conn, video_path, cfg)
    if not segments:
        return []
    return _fps_points({"samples": dense_anchor_points(subs, segments)})


def _dense_probe(pts: list) -> Optional[dict]:
    return stretch_probe([(a, a - c) for a, c in pts]) if pts else None


def _rescaled(subs, ratio: float, offset: float):
    """(copy of subs rescaled, worst move in seconds)."""
    out = copy.deepcopy(subs)
    return out, apply_fps_rescale(out, ratio, offset=offset)


def _try_rate_from_baseline(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path,
                            cfg: Config, media_root: Path, baseline, current):
    """Rate error fixed from the pre-sync file over the full transcript.

    alass answers a drift with one shift (or blocks) and leaves the ramp; the
    discrete path only knows 0.1%. Dense anchors on the untouched file read any
    rate and offset directly; a real conversion ratio is snapped to. Skipped when
    the current file is already flat. The fix must come out flat itself.
    Returns (fixed_subs, note_fragment, info) or None."""
    if not cfg.fps_check_enabled or cfg.dry_run or baseline is None:
        return None
    pts = _dense_pool(conn, video_path, baseline, cfg)
    p = _dense_probe(pts)
    if not rate_gates_pass(p):
        return None
    if current is not baseline and rate_is_flat(
            _dense_probe(_dense_pool(conn, video_path, current, cfg)), tight=True):
        return None
    ratio, offset, name = snap_rate(pts, p)
    # Second look from the coarse fix: the original only matches while its
    # offset fits the window (PAL: first ~20 min). Mapped back, whole file.
    coarse, _ = _rescaled(baseline, ratio, offset)
    full = [(a, c / ratio - offset) for a, c in _dense_pool(conn, video_path, coarse, cfg)]
    p_full = _dense_probe(full)
    if (p_full is not None and rate_gates_pass(p_full)
            and p_full["keep_frac"] >= p["keep_frac"] - 0.05):
        ratio, offset, name = snap_rate(full, p_full)
    fixed, worst = _rescaled(baseline, ratio, offset)
    after = _dense_probe(_dense_pool(conn, video_path, fixed, cfg))
    if not rate_is_flat(after):
        log.info("rate %s for %s discarded: not flat after (%s)", name, subtitle_path.name,
                 after and f"tilt {after['tilt']:+.2f}s, offset {after['intercept']:+.2f}s")
        return None
    if worst < cfg.min_change_seconds:
        return None
    _write_fix(subtitle_path, cfg, media_root, fixed)
    info = _stretch_info(name, ratio, p, worst)
    note = (f" rate {name} offset {offset:+.1f}s from the original file over the full "
            f"transcript (tilt {p['tilt']:+.1f}s over {p['n']} lines, rho {p['rho']:+.2f}, "
            f"{p['keep_frac']:.0%} on the line; flat after: tilt {after['tilt']:+.2f}s).")
    log.info("rate fix %s for %s:%s", name, subtitle_path.name, note)
    return fixed, note, info


def _rate_says_needs_full(collected: dict, cfg: Config) -> bool:
    """Sampled pool leans like a rate: buy the full transcript to measure it."""
    if cfg.whisper_mode != "sampled" or not cfg.fps_check_enabled \
            or collected.get("full_coverage"):
        return False
    p = _dense_probe(_fps_points(collected))
    return (p is not None and p.get("rho") is not None
            and abs(p["tilt"]) >= RATE_MIN_TILT_S and abs(p["rho"]) >= 0.6
            and p["keep_frac"] >= 0.8)


def _vad_says_needs_full(conn: sqlite3.Connection, video_path: Path, collected: dict, subs,
                         cfg: Config) -> bool:
    """Sampled clips miss blocks between them; VAD sees the whole file for CPU only."""
    if cfg.whisper_mode != "sampled" or not cfg.escalate_sampled_to_full:
        return False
    if collected.get("full_coverage") or collected.get("skipped"):
        return False
    from verifyarr.vad import speech_timeline, block_witness
    ivs = speech_timeline(video_path, cfg, conn)
    return bool(ivs) and block_witness(ivs, [(e.start / 1000.0, e.end / 1000.0)
                                             for e in subs.events])


def _swap_says_needs_full(collected: dict, subs, cfg: Config) -> bool:
    """Many heuristic hits buy the full-transcript look; the verdict needs it."""
    if cfg.whisper_mode != "sampled" or not cfg.escalate_sampled_to_full:
        return False
    if collected.get("full_coverage") or collected.get("skipped"):
        return False
    return len(heuristic_candidates(subs)) >= SWAP_GATE_ESCALATE_HEURISTIC


def _swap_gate_hit(conn: sqlite3.Connection, video_path: Path, subs, cfg: Config) -> Optional[dict]:
    """Timing-independent swap rate from the cached full transcript, else None."""
    segments = _cached_full_segments(conn, video_path, cfg)
    if not segments:
        return None
    return swap_gate_evidence(subs, segments)


def _screen_says_needs_full(collected: dict, cfg: Config, sync_blocks: Optional[int] = None) -> bool:
    """Whether a sampled run has seen enough to justify paying for the whole transcript.

    The cheap clips are a SCREEN: they answer "does this file's timing look wrong somewhere"
    well (measured over 52 episodes: a 4-probe screen flags 9/9 files that genuinely need
    re-timing, 1 false alarm in 43), but they cannot answer "wrong where, by how much" -- a
    region needs 3 agreeing anchors to plan from, and a handful of probes can't cover several
    regions. Measured: escalating with more probes plans correctly on 4 of 9 real files;
    escalating to the full transcript reaches 8 of 9. So the ladder skips the middle rung.

    Either signal is enough: probes disagreeing with EACH OTHER (a hop between them), or ANY
    single probe with a real residual. Deliberately a lower bar than the SUSPECT escalation's
    "3 agreeing residuals" -- that rule guards a VERDICT about the file, this one only decides
    whether to go and look properly, so one probe is reason enough. Measured over 52 episodes:
    the strict rule catches 5 of 10 bad files, this one 9 of 10, at the cost of 2 unnecessary
    transcriptions in 42 healthy files."""
    if cfg.whisper_mode != "sampled" or not cfg.escalate_sampled_to_full:
        return False
    # The full transcript is only worth buying where it can drive a REPAIR, and the only repair
    # it drives is region planning -- which needs the file to have regions. alass has already
    # said whether it does, for free: a multi-block fit means it could not line the file up with
    # one offset. Measured over 2086 sampled rows, multi-block fires on 75% of piecewise files
    # and 0% of clean, gap, swap and uniform ones. Detection does not need this branch at all
    # (see anchor_suspect_min_samples).
    samples = collected.get("samples") or []
    # One anchor 2.5s+ out is a block witness (sparse sampling lands 0-1 clips
    # in a 180s block). Dense evidence decides -- this only buys the look.
    if significant_anchor_residuals(samples, ANCHOR_SUSPECT_THRESHOLD_S, min_samples=1):
        return True
    # Three matched lines in a row at one offset: the block verdict needs more lines.
    if anchor_point_runs(samples, k=3):
        return True
    if cfg.escalate_only_multi_block and not (sync_blocks or 0) > 1:
        return False
    # The multi-block fit IS the suspicion. A half-repaired block file's sampled
    # anchors agree with each other -- the displaced text never anchors -- so
    # asking them to disagree too let those files through silently.
    if (sync_blocks or 0) > 1:
        return True
    spread = anchor_spread(samples)
    return spread is not None and spread > ANCHOR_SCREEN_SPREAD_S


def _recheck_after_resync(video_path: Path, corrected_subs, lang: Optional[str], cfg: Config,
                           conn: sqlite3.Connection, previous_collected: dict, previous_result: dict,
                           cancel_event=None) -> tuple:
    """Re-runs the correctness/line-order evidence pass against a subtitle _try_anchor_resync just
    re-timed. Falls back to the previous pass's results if the fresh one can't be produced -- a
    failure here must not undo a correction that already verified."""
    try:
        with tempfile.TemporaryDirectory() as td:
            if cfg.whisper_mode == "full":
                collected = collect_samples_full(video_path, corrected_subs, lang, cfg, Path(td),
                                                  conn, cancel_event=cancel_event)
            else:
                collected = collect_samples(video_path, corrected_subs, lang, cfg, Path(td),
                                             conn=conn, cancel_event=cancel_event)
    except JobCancelled:
        raise
    except Exception as e:
        log.warning("Post-resync re-check failed for %s: %s", video_path.name, e)
        return previous_collected, previous_result, previous_result.get("swap_severity")
    if collected.get("skipped"):
        return previous_collected, previous_result, previous_result.get("swap_severity")
    act = cfg.line_order_enabled and cfg.line_order_audio_confirm
    result = finalize_line_order(collected, cfg, cancel_event=cancel_event, compute_swap_severity=act)
    return collected, result, result.get("swap_severity")


def _apply_line_order(row: dict, result: dict, swap_severity, current_subs, subtitle_path: Path,
                       cfg: Config, media_root: Path) -> None:
    """Records what the line-order evidence found. Reports only -- nothing is rewritten.

    The per-cue repair used to run here. It cost 40-61 rewritten cues per Community file for a
    cross-model agreement of 0.02-0.12 Jaccard, i.e. it mostly rewrote lines the next model would
    not have touched (B1/B4).

    The file-level rate does not justify the other action either -- measured over 4200 matrix rows,
    the best threshold that catches 96% of files with injected swaps also fires on 45% of healthy
    ones (22% false alarms at best, in sampled mode, for 65% detection). There is no valley between
    the two distributions, so there is nothing to act on: a widespread pattern is reported, never
    acted on. Both lists survive because a Whisper-confirmed swap and an untested heuristic hit are
    different evidence to whoever reads the note."""
    issues = result.get("line_issues") or []
    flagged = result.get("line_flagged") or []
    row["line_order_fixed"] = 0
    row["line_order_flagged"] = len(issues) + len(flagged)
    row["line_order_swap_rate"] = result.get("swap_rate")
    if issues:
        widespread_note = ""
        if swap_severity:
            widespread_note = (f", {swap_severity['whisper_confirmed']}/"
                               f"{swap_severity['whisper_checked']} of the tested candidates")
        row["note"] += (f" Line order: {len(issues)} block(s) confirmed swapped by Whisper"
                         f"{widespread_note} — reported, not repaired.")
    if flagged:
        row["note"] += (f" Line order: {len(flagged)} block(s) flagged for manual "
                         f"review (unconfirmed).")


def _row_cost(row: dict) -> dict:
    """This file's whole Whisper bill: the correctness phase plus what the screen spent
    before it. The screen's share travels in the row because a sweep charges it one batch
    ahead of this call, so the module-level counter alone is not one file's total."""
    pre = row.pop("_screen_cost", None) or {}
    now = whisper_cost.snapshot()
    return {k: round(v + (pre.get(k) or 0.0), 1) for k, v in now.items()}


def _detection_note(conn: sqlite3.Connection, video_path: Path, current_subs, cfg: Config,
                    row: dict, result: dict, resolved_winner: Optional[str],
                    escalated_samples: Optional[list], lang: Optional[str],
                    cancel_event=None) -> Optional[str]:
    """The last detectors of the verdict chain, in order; the first note that fires, or None.
    Each is evaluated only when the ones before it stayed quiet."""
    have_full = bool(result.get("full_coverage")) or escalated_samples is not None
    runs = (_block_runs_hit(conn, video_path, current_subs, cfg,
                            proven_block=resolved_winner == "blocks")
            if have_full or cfg.whisper_mode == "full" else [])
    if runs:
        # Part of the file sits at another offset.
        return (f" Part of the episode is out of sync: {_runs_text(runs)} -- fetch a fresh "
                "subtitle.")
    parts = _block_repair_parts(row)
    if parts >= 2:
        return BLOCK_REPAIR_NOTE.format(n=parts)
    jit = anchor_jitter((result.get("samples") if result.get("full_coverage")
                         else escalated_samples) or [])
    if jit is not None and jit >= JITTER_MIN_MAD_S:
        # Per-cue noise has no offset to fix. Full coverage only.
        return (f" Cue timing is noisy: lines within one clip disagree by {jit:.2f}s "
                "(median) -- no single offset fixes that; fetch a fresh subtitle.")
    if have_full:
        mm = _missing_middle_hit(conn, video_path, current_subs, cfg)
    elif cfg.whisper_mode == "sampled":
        mm = _missing_middle_probe(video_path, current_subs, cfg,
                                   result.get("audio_lang") or lang, cancel_event=cancel_event)
    else:
        mm = None
    if mm is not None:
        # The surviving timings are correct, nothing to fix.
        return (f" Subtitle has no lines for {mm['gap_end'] - mm['gap_start']:.0f} s "
                f"at {_mmss(mm['gap_start'])}-{_mmss(mm['gap_end'])} where the audio "
                f"has {mm['speech_s']:.0f} s of speech -- part of the episode is "
                "missing; fetch a fresh subtitle.")
    return None


def _gather_evidence(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
                     conn: sqlite3.Connection, row: dict, current_subs, cancel_event=None):
    """(collected, ev_cfg, reused_cache, cache_key) for one file: the cached clips when the
    subtitle is unchanged, else fresh ones; plus the full transcript when an escalation
    trigger fires. collected is {"skipped": True, ...} when the check could not run."""
    cache_key = cache_key_for(current_subs, cfg)
    # Evidence config: becomes full-mode once an escalation has bought the transcript.
    # Cache keys stay on cfg, so the next sampled run still hits.
    ev_cfg = cfg
    cached = db.get_line_order_cache(conn, subtitle_path)
    reused_cache = bool(cached and cached["key"] == cache_key)
    try:
        if reused_cache:
            collected = _cache_load(cached["json"])
            if collected.get("full_coverage"):
                ev_cfg = dataclasses.replace(cfg, whisper_mode="full")
            # Older cache rows predate heuristic_indices -- absent means "no filtering" to
            # finalize_line_order, which is what those rows' (heuristic-only) candidates
            # already were anyway.
        elif cfg.whisper_mode == "full":
            # Dense, whole-file coverage already -- no need for sync_pair's targeted extra
            # samples around alass's block boundaries (collect_samples' extra_target_ranges);
            # a full transcript already covers every block on its own.
            with tempfile.TemporaryDirectory() as td2:
                collected = collect_samples_full(video_path, current_subs, lang, cfg, Path(td2),
                                                  conn, cancel_event=cancel_event)
        else:
            # Peeked, not popped -- the actual resolution (which candidate wins) happens
            # further down, after this Whisper pass; this only needs the block time-ranges
            # to make sure sampling doesn't miss one of them entirely (see sync_pair's
            # _block_time_ranges and collect_samples' extra_target_ranges).
            extra_target_ranges = (row.get("_ambiguous_sync") or {}).get("blocks_time_ranges") or None
            with tempfile.TemporaryDirectory() as td2:
                collected = collect_samples(video_path, current_subs, lang, cfg, Path(td2),
                                             conn=conn, cancel_event=cancel_event,
                                             extra_target_ranges=extra_target_ranges)
        # Checked even on a reused cache hit, not just a freshly-sampled one -- cache_key_for
        # doesn't vary with escalate_sampled_to_full, so a file screened negative before that
        # setting was turned on (or a borderline one) would otherwise never get re-screened
        # until its subtitle content itself changes. _screen_says_needs_full is a no-op
        # (returns False) outside sampled mode, so this is safe to call unconditionally.
        # A deferred multi-block fit reports sync_split_blocks = 1 (that is the single-offset
        # candidate sync_pair held back); the real block count is in the deferred payload.
        deferred = row.get("_ambiguous_sync") or {}
        blocks = deferred.get("blocks_split_count") or row.get("sync_split_blocks")
        # First trigger wins; later ones are not evaluated (same order as always).
        ladder = (
            (lambda: _screen_says_needs_full(collected, cfg, blocks),
             "sampled clips disagree about the timing -- re-checking against a full transcript"),
            (lambda: (_fps_says_needs_full(collected, cfg)
                      or _rate_says_needs_full(collected, cfg)),
             "pooled anchors show a global drift signature -- confirming against a full transcript"),
            (lambda: _jitter_says_needs_full(collected, cfg),
             "sampled anchors are noisy -- confirming against a full transcript"),
            (lambda: _swap_says_needs_full(collected, current_subs, cfg),
             "many suspected swapped lines -- confirming against a full transcript"),
            (lambda: _vad_says_needs_full(conn, video_path, collected, current_subs, cfg),
             "speech pattern suggests a block -- confirming against a full transcript"),
        )
        why = next((msg for hit, msg in ladder if hit()), None)
        if why is not None:
            log.info("%s: %s", subtitle_path.name, why)
            with tempfile.TemporaryDirectory() as td2:
                full = collect_samples_full(video_path, current_subs, lang, cfg, Path(td2),
                                             conn, cancel_event=cancel_event)
            if not full.get("skipped"):
                collected = full
                # Resync and recheck must use it too -- they fell back to the 16
                # sampled clips and judged the repair on those.
                ev_cfg = dataclasses.replace(cfg, whisper_mode="full")
    except JobCancelled:
        raise  # a cancelled job is not a "skipped" check -- let jobs.py end the run cleanly
    except Exception as e:
        log.warning("Correctness/line-order check failed for %s: %s", subtitle_path, e)
        collected = {"skipped": True, "reason": str(e)}
    return collected, ev_cfg, reused_cache, cache_key


def correctness_and_finish(video_path: Path, subtitle_path: Path, lang: Optional[str],
                            cfg: Config, conn: sqlite3.Connection, row: dict, current_subs,
                            bazarr_meta: Optional[dict] = None,
                            history_index: Optional[dict] = None,
                            run_id: Optional[int] = None, cancel_event=None) -> dict:
    """Correctness/line-order check (Whisper) + handle_suspect + persisting state — everything
    sync_pair above doesn't do. Needs `conn`, so unlike sync_pair this must run on whichever
    thread owns that sqlite3 connection (see jobs._run_sweep, which keeps this stage sequential
    even though the sync stage runs in a thread pool — Groq's rate-limit pacing and this app's
    single-job cancellation both also expect one file at a time here)."""
    media_root = cfg.media_root_for(subtitle_path)

    def act_on_suspect():
        """The configured auto-action for a SUSPECT file (quarantine/blacklist/remediate)."""
        return handle_suspect(subtitle_path, video_path, cfg, media_root, lang, bazarr_meta,
                              history_index, cfg.correctness_auto_action, conn=conn,
                              run_id=run_id, cancel_event=cancel_event)
    # Per file, so the row can report what this check actually cost -- and what the caches saved.
    # The screen's own spend is carried in the row (see _row_cost) rather than left in the
    # counter: in a sweep it was charged before this file's turn came round.
    whisper_cost.reset()
    # Popped immediately: it's a pysubs2 object sync_pair left for _try_anchor_resync, and nothing
    # that gets persisted or serialized may still be carrying it (see apply_pending_sync).
    pre_sync_subs = row.pop("_pre_sync_subs", None)
    orig_subs = row.pop("_orig_subs", None)
    if current_subs is None:
        # sync_pair couldn't parse the original subtitle at all -- nothing to correctness-check.
        row["whisper_cost"] = _row_cost(row)
        update_state(conn, video_path, subtitle_path, row)
        return row

    # Whether a Whisper-based correctness check can run at all this file, independent of whether
    # line-order is turned on.
    correctness_unavailable_flag = None
    if not cfg.enable_correctness_check:
        correctness_unavailable_flag = "disabled"
    elif not cfg.has_stt_configured:
        correctness_unavailable_flag = f"no {cfg.stt_provider} API key"

    if correctness_unavailable_flag is None:
        # Whenever correctness runs, collect line-order candidate/Whisper-verdict data too (see
        # line_order.py module docstring) — it rides on the same clips correctness is already
        # sending, so there's no reason not to, regardless of whether line-order is turned on.
        # Cached (line_order.cache_key_for, keyed on the subtitle's own content) so a run against
        # an unchanged subtitle reuses it instead of re-transcribing — including a LATER run where
        # the user has since turned line-order on, which then only has to pay for the LLM
        # confirmation step below, not for Whisper again.
        collected, ev_cfg, reused_cache, cache_key = _gather_evidence(
            video_path, subtitle_path, lang, cfg, conn, row, current_subs,
            cancel_event=cancel_event)

        if collected.get("skipped"):
            row["correctness_flag"] = "skipped"
            row["note"] = (row["note"] + " | " + collected.get("reason", "")).strip(" |")
        else:
            if reused_cache:
                log.info("Reusing cached correctness/line-order data for %s (subtitle unchanged "
                          "since last check).", subtitle_path.name)
            else:
                row["line_order_cache_key"] = cache_key
                row["line_order_cache_json"] = _cache_json(collected)

            act_on_line_order = cfg.line_order_enabled and cfg.line_order_audio_confirm
            result = finalize_line_order(collected, cfg, cancel_event=cancel_event,
                                          compute_swap_severity=act_on_line_order)
            row["correctness_avg_score"] = round(result["avg_score"], 3) if result["avg_score"] is not None else None
            row["correctness_audio_lang"] = result.get("audio_lang")
            row["correctness_samples"] = result.get("samples")
            swap_severity = result.get("swap_severity")

            # Many swapped lines: SUSPECT before anything is written. Sync only
            # moves cues; the text is identical across candidates, so this
            # measures the file as-is. Deferred sync is dropped unwritten.
            swap_ev = _swap_gate_hit(conn, video_path, current_subs, cfg)
            if swap_ev is not None and swap_gate_trips(swap_ev):
                had_deferred = row.pop("_ambiguous_sync", None) is not None
                if had_deferred or not (row.get("sync_status") or "").startswith("fixed"):
                    row["sync_status"] = "left unchanged (many swapped lines -- not corrected)"
                    row["sync_max_shift_s"] = None
                    row["sync_split_blocks"] = None
                    row["sync_block_spread_s"] = None
                row["correctness_flag"] = "SUSPECT"
                row["note"] = (row["note"] + f" Many swapped lines ({swap_ev['swapped']} of "
                               f"{swap_ev['checked']} tested) -- fetch a fresh subtitle.").strip()
                row["line_order_fixed"] = 0
                row["line_order_flagged"] = len(result.get("line_issues") or []) \
                    + len(result.get("line_flagged") or [])
                row["line_order_swap_rate"] = result.get("swap_rate")
                row["auto_action"] = act_on_suspect()
                row["whisper_cost"] = _row_cost(row)
                update_state(conn, video_path, subtitle_path, row, run_id=run_id,
                             media_root=media_root)
                return row

            # A fix sync_pair deferred writing (an alass result held back together
            # with its alternative — see its own docstring) gets resolved HERE,
            # before any of the branches
            # below act on `result` -- the winner might not even be "new" (the candidate
            # `result` currently describes), so nothing downstream should judge/act on "new"
            # until this has had a chance to replace it.
            ambiguous = row.pop("_ambiguous_sync", None)
            # The deferred path keeps the pre-alass baseline here instead of in _pre_sync_subs
            # (sync_pair returns before it gets that far) -- either way it is what
            # _try_anchor_resync falls back to when alass's own fit can't be planned from.
            if ambiguous is not None and pre_sync_subs is None:
                pre_sync_subs = ambiguous.get("old_subs")
            if ambiguous is not None and orig_subs is None:
                orig_subs = ambiguous["orig_subs"]
            resolved_winner = None
            # Resolution below may hand back sparse samples; the step test needs these.
            escalated_samples = result.get("samples") if result.get("full_coverage") else None
            if ambiguous is not None:
                current_subs, result, swap_severity, resolved_winner = _resolve_ambiguous_sync(
                    # cfg, not ev_cfg: on the full evaluator it destroyed 4 good block
                    # repairs (0.98 -> 0.74). stepped_before keeps the full samples instead.
                    conn, video_path, subtitle_path, lang, cfg, media_root, ambiguous, result, row,
                    cancel_event=cancel_event)
                row["correctness_avg_score"] = round(result["avg_score"], 3) if result["avg_score"] is not None else None
                row["correctness_audio_lang"] = result.get("audio_lang")
                row["correctness_samples"] = result.get("samples")
            if row.pop("_vetoed_bad_fit", False):
                # Anchors disproved alass' fit; the original was kept unwritten.
                # Flagged, not silently kept: the file alass moved is suspect.
                row["correctness_flag"] = "SUSPECT"
                row["note"] = (row["note"] + " Fetch a fresh subtitle.").strip()
                row["line_order_fixed"] = 0
                row["line_order_flagged"] = len(result.get("line_issues") or []) \
                    + len(result.get("line_flagged") or []) \
                    if isinstance(result, dict) else 0
                row["auto_action"] = act_on_suspect()
                row["whisper_cost"] = _row_cost(row)
                update_state(conn, video_path, subtitle_path, row, run_id=run_id,
                             media_root=media_root)
                return row

            # A 24fps subtitle on 23.976fps audio (or the reverse) drifts ~1s per 17
            # minutes -- content-correct everywhere, so every branch below says "ok"
            # while the file slowly walks out of sync. _try_fps_rescale catches it by
            # the discrete ratio, but only when pooled anchors AND the VAD timeline
            # agree on the tilt; like the anchor resync below, the evidence above was
            # gathered against cue times this fix just changed, so re-gather before
            # the verdict chain judges the corrected file.
            ramp_probe_saved = row.pop("_ramp_rescued", None)
            fps_fix = _try_rate_from_baseline(conn, video_path, subtitle_path, cfg, media_root,
                                              orig_subs or current_subs,
                                              current_subs)
            if fps_fix is None:
                fps_fix = _try_fps_rescale(conn, video_path, subtitle_path, lang, cfg,
                                           media_root, current_subs, result)
            if fps_fix is None and ramp_probe_saved is not None:
                # The normal path cannot fire here (new's SUSPECT flag, or a
                # second-path pool under the strict keep bar) -- but the rescue
                # already verified this probe end to end, so apply it directly.
                fps_fix = _apply_stretch_fix(subtitle_path, cfg, media_root,
                                             current_subs, _fps_points(result),
                                             ramp_probe_saved)
            if fps_fix is not None:
                current_subs, fps_note, fps_info = fps_fix
                row["fps_ratio"] = fps_info["name"]
                row["sync_max_shift_s"] = round(fps_info["worst"], 2)
                # The rate fix replaced alass' file: its block fields no longer apply.
                row["sync_split_blocks"] = None
                row["sync_block_spread_s"] = None
                # Says the file was REWRITTEN. Without it a framerate fix leaves sync_status on
                # "already in sync", and every "healthy files untouched" count -- the matrix's
                # included -- reads a rewritten file as untouched.
                kind = "rate" if fps_info.get("kind") == "stretch" else "framerate"
                row["sync_status"] = f"fixed ({kind} {fps_info['name']}, up to {fps_info['worst']:.1f}s)"
                row["note"] = (row["note"] + fps_note).strip()
                pre_recheck_collected = collected
                collected, result, swap_severity = _recheck_after_resync(
                    video_path, current_subs, lang, ev_cfg, conn, collected, result,
                    cancel_event=cancel_event)
                # The row must describe the rescaled file, not the one before it.
                row["correctness_samples"] = result.get("samples")
                # Same staleness guard as the anchor-resync path: only cache under the
                # corrected fingerprint when the recheck actually re-gathered evidence
                # (on failure/skip it returns the SAME pre-fix object).
                if collected is not pre_recheck_collected:
                    row["line_order_cache_key"] = cache_key_for(current_subs, cfg)
                    row["line_order_cache_json"] = _cache_json(collected)

            # Shared by the safety net and the anchor branch below: a file the
            # anchors condemn always gets the resync ATTEMPT first -- the net only
            # fires when the anchors have nothing to plan from.
            # 2 of ~51 full anchors is noise (SH_S01E04 fired on exactly that),
            # so dense evidence needs 3. A resolved old/blocks winner keeps 2:
            # block structure is already proven there, and a sampled resolution
            # judges on sparse cached clips anyway.
            dense = (ev_cfg.whisper_mode == "full"
                     and resolved_winner in (None, "new"))
            suspect_min = (ANCHOR_SUSPECT_MIN_SAMPLES if dense
                           else cfg.anchor_suspect_min_samples)
            anchor_bad = (significant_anchor_residuals(
                result.get("samples") or [], ANCHOR_SUSPECT_THRESHOLD_S,
                min_samples=suspect_min)
                if cfg.anchor_check_enabled else [])
            if (cfg.anchor_check_enabled and not anchor_bad
                    and row["sync_block_spread_s"] is not None
                    and row["sync_block_spread_s"] >= cfg.block_spread_suspect_threshold_s
                    and (resolved_winner is None or resolved_winner == "blocks")):
                anchor_bad = [s for s in result.get("samples") or []
                              if s.get("anchor")
                              and abs(s["anchor"]["shift"]) >= ANCHOR_HUGE_SINGLE_S
                              and s.get("score") is not None
                              and s["score"] >= cfg.overlap_threshold]
            if result["flag"] == "SUSPECT":
                row["correctness_flag"] = "SUSPECT"
                excerpts = " || ".join(
                    f"{s['start']}s: \"{s.get('transcript_excerpt', s.get('error', ''))}\""
                    for s in result["samples"]
                )
                row["note"] = (row["note"] + " Whisper heard: " + excerpts).strip()
                row["auto_action"] = act_on_suspect()
            elif (row["sync_block_spread_s"] is not None
                  and row["sync_block_spread_s"] >= cfg.block_spread_suspect_threshold_s
                  and not anchor_bad
                  and (resolved_winner is None or resolved_winner == "blocks")
                  and any(s.get("score") is not None and s["score"] < cfg.overlap_threshold
                          for s in result["samples"])):
                # A safety net for a multi-block sync the evidence can't confirm: either
                # sync_pair applied it directly (its --no-split alternative failed, or the
                # comparison couldn't run), or the {original, single-offset, multi-block}
                # comparison above KEPT 'blocks' -- a relative win ("best of three") that
                # still leaves whole stretches unconfirmed. The resolved-winner half is
                # symmetry, not a measured case: no matrix row fires it (kept-'blocks'
                # rows either carry anchor evidence, which defers to the anchor branch,
                # or match their windows everywhere).
                # The majority-vote check alone (_aggregate_correctness) said "ok" -- but alass
                # itself needed wildly different offsets in different parts of this file to line
                # up the audio TIMING (real cuts can cause that on their own), AND at least one
                # Whisper sample independently disagreed with the CONTENT in its own window.
                # Neither signal alone is trusted (a real cut can produce a big spread with every
                # sample still matching; one bad sample alone is exactly what majority-vote is
                # designed to overrule) -- both agreeing is what escalates it.
                row["correctness_flag"] = "SUSPECT"
                failing = [s for s in result["samples"]
                           if s.get("score") is not None and s["score"] < cfg.overlap_threshold]
                kept = ("kept 'blocks' over its single-offset fit, but "
                        if resolved_winner == "blocks" else "needed ")
                row["note"] = (row["note"] +
                                f" Escalated to SUSPECT: alass {kept}a {row['sync_block_spread_s']:.1f}s-spread "
                                f"multi-block sync AND {len(failing)}/{len(result['samples'])} Whisper sample(s) "
                                "didn't match their window on their own -- majority-vote alone wasn't enough to "
                                "trust this file.").strip()
                row["auto_action"] = act_on_suspect()
            elif anchor_bad:
                bad = anchor_bad
                # A Whisper anchor (subtitles.clip_anchor_shift) is a CONTENT-verified point
                # estimate of the true timing offset at one exact instant -- not the bag-of-
                # words window score every other branch here relies on, which can be fooled by
                # shared vocabulary (character names, series jargon) scoring "ok" even against
                # the wrong episode. A confident anchor still showing a residual mismatch above
                # the anchor noise floor (ANCHOR_SUSPECT_THRESHOLD_S, NOT min_change_seconds)
                # means majority-vote/whole-file averaging missed a genuine problem at that
                # specific point (see Config.anchor_check_enabled).
                worst = max(abs(s["anchor"]["shift"]) for s in bad)
                where = ", ".join(f"{s['start']}s (Δ{s['anchor']['shift']:.1f}s)" for s in bad)
                # The anchors just measured the error. Try to CORRECT it before giving up on the
                # file -- see _try_anchor_resync. Only reachable with result["flag"] == "ok", i.e.
                # the text is this episode's; a wrong-episode file never gets here.
                # Whether the file STEPPED before the repair. Measured on the corrected
                # file this is invisible (0 of 22 half-repaired rows still step at the
                # sampled points, while healthy files never do), so the repair cannot be
                # asked to disprove it afterwards -- the suspicion has to travel.
                stepped_before = bool(anchor_slope_breaks(result.get("samples") or [])
                                      or anchor_slope_breaks(escalated_samples or []))
                resynced = None
                if cfg.anchor_resync_enabled and result["flag"] == "ok" and not cfg.dry_run:
                    resynced = _try_anchor_resync(conn, video_path, subtitle_path, lang, ev_cfg,
                                                   media_root, current_subs, result.get("audio_lang"),
                                                   fallback_subs=pre_sync_subs,
                                                   cancel_event=cancel_event)
                if resynced is not None:
                    current_subs, resync_note, plan = resynced
                    row["correctness_flag"] = "ok"
                    row["sync_status"] = (f"fixed (Δ{max(abs(r['shift']) for r in plan):.1f}s, "
                                          f"{len(plan)} anchor region(s))")
                    row["sync_max_shift_s"] = round(max(abs(r["shift"]) for r in plan), 2)
                    row["note"] = (row["note"] + resync_note).strip()
                    # The line-order evidence above was gathered against the cue times this
                    # resync just changed, so it describes a file that no longer exists -- it
                    # searched the transcript at timestamps up to 54s from where the speech
                    # actually is. Re-gather against the corrected file before acting on it
                    # (measured: 92 -> 111 decidable candidates on C_S03E01, 37 -> 57 on
                    # C_S02E21). Free in full mode: the transcript is already cached, this is
                    # token overlap, not a new Whisper call.
                    pre_recheck_collected = collected
                    collected, result, swap_severity = _recheck_after_resync(
                        video_path, current_subs, lang, ev_cfg, conn, collected, result,
                        cancel_event=cancel_event)
                    # Set above from the PRE-resync evidence, which describes a file that
                    # no longer exists once the resync has written. Anything reading this
                    # row afterwards should see what was actually judged.
                    row["correctness_samples"] = result.get("samples")
                    # The resync verified itself on its own sparse anchors, excluding
                    # +-clip_seconds around every planned cut -- but that check passes
                    # while whole cue bands around misplaced cuts are still mistimed
                    # (measured: C_S03E03/C_S03E08 full land at rec 0.87-0.88 with
                    # 3-4 bad recheck anchors, flag ok). Re-judge the CORRECTED file
                    # on the fresh recheck evidence -- the same significance rule that
                    # condemned it, with no cut exclusions. The improvement stays on
                    # disk either way; only the verdict changes.
                    resync_still_bad = significant_anchor_residuals(
                        result.get("samples") or [], ANCHOR_SUSPECT_THRESHOLD_S,
                        min_samples=cfg.anchor_suspect_min_samples)
                    # Counting BAD anchors misses a half-repaired file: the stretch still
                    # wrong anchors fine against its own displaced text, so this rule fired
                    # on 0 of 30 silent block rows. A step in the audio->cue mapping is the
                    # same damage seen as a shape instead of a count.
                    slope_breaks = anchor_slope_breaks(result.get("samples") or [])
                    # A file that stepped before the repair has a block error, and the
                    # repair only settles the regions its anchors reached -- a block with
                    # no anchor of its own is absorbed into a neighbour and keeps that
                    # neighbour's offset. The corrected file cannot show this (see
                    # stepped_before), so an unresolved step is proven by absence of
                    # proof, not by evidence: keep the fix, warn about the rest.
                    unproven_step = stepped_before and not slope_breaks and not resync_still_bad
                    # The remainder a repair leaves under every threshold: a run of anchors
                    # 1.5-2.5s off, silent on 3 of 3 such matrix rows before this.
                    run_offsets = anchor_run_offsets(result.get("samples") or [])
                    # Lines still running together off the file: a half-done repair.
                    block_left = _block_runs_hit(conn, video_path, current_subs, ev_cfg,
                                                 proven_block=True)
                    if (result.get("flag") != "ok" or resync_still_bad
                            or slope_breaks or unproven_step or run_offsets
                            or block_left or len(plan) >= 2):
                        if result.get("flag") not in ("ok", "SUSPECT"):
                            row["correctness_flag"] = "unknown"
                            row["note"] = (row["note"] + " Correctness could not be "
                                           "determined after anchor resync: "
                                           f"{result['flag']}.").strip()
                        else:
                            row["correctness_flag"] = "SUSPECT"
                            if resync_still_bad:
                                worst = max(abs(s["anchor"]["shift"])
                                            for s in resync_still_bad)
                                where = ", ".join(
                                    f"{s['start']}s (Δ{s['anchor']['shift']:.1f}s)"
                                    for s in resync_still_bad)
                                row["note"] = (row["note"] +
                                               f" Timing mismatch REMAINS after anchor resync: "
                                               f"{len(resync_still_bad)} Whisper anchor(s) still show "
                                               f"up to {worst:.1f}s at [{where}] on the corrected "
                                               f"file.").strip()
                            elif slope_breaks:
                                where = ", ".join(f"{b['at']}s ({b['step']:+.1f}s)"
                                                  for b in slope_breaks[:4])
                                row["note"] = (row["note"] +
                                               f" Block boundary REMAINS after anchor resync: the "
                                               f"corrected file still steps at {len(slope_breaks)} "
                                               f"point(s) [{where}] where the audio runs on -- part "
                                               f"of the episode is still mistimed.").strip()
                            elif run_offsets:
                                where = ", ".join(f"{r['from']:.0f}-{r['to']:.0f}s ({r['dev']:+.1f}s)"
                                                  for r in run_offsets[:4])
                                row["note"] = (row["note"] +
                                               f" Block remainder after anchor resync: a run of "
                                               f"anchors still sits off the rest of the file at "
                                               f"[{where}] -- part of the episode is still "
                                               f"mistimed.").strip()
                            elif unproven_step:
                                row["note"] = (row["note"] + " Block error repaired, but NOT "
                                               "verified across the whole episode: the file "
                                               "stepped before the resync, and the corrected "
                                               "file gives no anchor that proves every block "
                                               "was moved -- check it, or fetch a fresh "
                                               "subtitle.").strip()
                            elif block_left:
                                row["note"] = (row["note"] + " Block REMAINS after anchor "
                                               f"resync: {_runs_text(block_left)} -- part of "
                                               "the episode is still mistimed.").strip()
                            elif len(plan) >= 2:
                                row["note"] = (row["note"]
                                               + BLOCK_REPAIR_NOTE.format(n=len(plan))).strip()
                            else:
                                row["note"] = (row["note"] + " Correctness check failed "
                                               "on the resynced file.").strip()
                            row["auto_action"] = act_on_suspect()
                    elif act_on_line_order:
                        _apply_line_order(row, result, swap_severity, current_subs,
                                           subtitle_path, cfg, media_root)
                    # Cache under the CORRECTED subtitle's own fingerprint, so the next run
                    # reuses this pass rather than the stale pre-resync one -- but only when the
                    # recheck actually re-gathered evidence. On failure/skip it falls back to
                    # returning the SAME object it was passed (`is`, not equality), still
                    # timestamped against the pre-resync cues; caching that under the corrected
                    # subtitle's fingerprint would make the next run treat stale evidence as
                    # valid forever, since nothing else invalidates it once cached. Leaving no
                    # cache entry means the next run just re-collects properly instead.
                    if collected is not pre_recheck_collected:
                        row["line_order_cache_key"] = cache_key_for(current_subs, cfg)
                        row["line_order_cache_json"] = _cache_json(collected)
                else:
                    row["correctness_flag"] = "SUSPECT"
                    row["note"] = (row["note"] +
                                    f" Escalated to SUSPECT: {len(bad)} Whisper anchor(s) show a confirmed "
                                    f"timing mismatch of up to {worst:.1f}s at [{where}], even though "
                                    "the overall average passed.").strip()
                    row["auto_action"] = act_on_suspect()
            elif result["flag"] != "ok":
                # _aggregate_correctness couldn't score a single sample ("unknown (no valid
                # samples)") -- every window failed, typically a foreign-language subtitle whose
                # LLM translation never came back. That is "nothing was verified", and it used to
                # fall through to the "ok" branch below and be recorded as a passed check.
                row["correctness_flag"] = "unknown"
                row["note"] = (row["note"] + f" Correctness could not be determined: {result['flag']}.").strip()
            elif (detected := _detection_note(conn, video_path, current_subs, cfg, row, result,
                                              resolved_winner, escalated_samples, lang,
                                              cancel_event)) is not None:
                # Detection only (arm 2): flagged, never rewritten.
                row["correctness_flag"] = "SUSPECT"
                row["note"] = (row["note"] + detected).strip()
                row["auto_action"] = act_on_suspect()
            else:
                row["correctness_flag"] = "ok"
                if act_on_line_order:
                    _apply_line_order(row, result, swap_severity, current_subs, subtitle_path,
                                       cfg, media_root)
                elif cfg.line_order_enabled:
                    # audio_confirm off — heuristic-only reporting, never auto-fixed. candidates
                    # is every 2-line event in full mode (see collect_samples_full) -- use
                    # heuristic_indices here instead, same free-heuristic-only count as before.
                    issues = collected.get("heuristic_indices")
                    if issues is None:
                        issues = collected["candidates"]
                    row["line_order_fixed"] = 0
                    row["line_order_flagged"] = len(issues)
                    if issues:
                        row["note"] += (f" Line order: {len(issues)} block(s) flagged for manual "
                                         f"review (not auto-fixed, low confidence).")
                # else: line-order is off entirely — nothing surfaced, but the data above is still
                # cached for whenever it's turned on.

    else:
        row["correctness_flag"] = correctness_unavailable_flag
        if cfg.line_order_enabled:
            # No correctness check running at all — free local heuristic only, no Whisper spent,
            # and nothing to reuse from a cache either (there's no prior Whisper data to check).
            issues = heuristic_candidates(current_subs)
            row["line_order_fixed"] = 0
            row["line_order_flagged"] = len(issues)
            if issues:
                row["note"] += (f" Line order: {len(issues)} block(s) flagged for manual "
                                 f"review (not auto-fixed, low confidence).")

    # Every path above that actually ran the Whisper check has resolved a deferred sync by now
    # (_resolve_ambiguous_sync). Any path that didn't (check disabled/unavailable, skipped for
    # lack of duration/wrong audio language, failed) must still put SOMETHING on disk -- the
    # safe default -- rather than leave the file unsynced under a "[pending]" status.
    if "_ambiguous_sync" in row:
        apply_pending_sync(subtitle_path, cfg, row, reason=f"correctness check: {row.get('correctness_flag')}")
    row["whisper_cost"] = _row_cost(row)
    update_state(conn, video_path, subtitle_path, row, run_id=run_id, media_root=media_root)
    return row
