"""Core per-file flow — process_pair runs sync (alass) + correctness check (Whisper) for one
video/subtitle pair, and handle_suspect deals with the result if it gets flagged SUSPECT.
Split into sync_pair (alass only, no DB access) + correctness_and_finish (Whisper + DB) so
jobs._run_sweep can run the sync half across a thread pool while keeping the correctness half
sequential — process_pair itself just calls the two back to back, for callers that don't need
them split (CLI, the Bazarr-hook single-file path, remediate's own candidate verification)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr.settings import Config
from verifyarr.subtitles import (
    load_subs, max_shift_stats, summarize_anchor_samples, ANCHOR_PREFER_MARGIN_S,
    ANCHOR_SUSPECT_THRESHOLD_S,
)
from verifyarr.sync_engine import (
    resolve_alass_bin, resolve_alass_reference, run_alass, parse_alass_shift_blocks,
    parse_alass_shift_blocks_with_counts,
)
from verifyarr.line_order import heuristic_candidates, collect_samples, finalize_line_order, cache_key_for, apply_line_swap
from verifyarr.correctness import (
    evaluate_against_cached_transcripts, significant_anchor_residuals, JobCancelled,
)
from verifyarr.fileops import backup_subtitle, quarantine_subtitle
from verifyarr.bazarr import (
    bazarr_map_path, bazarr_blacklist, remediate_suspect, remediate_without_history,
    request_replacement_fire_and_forget,
)
from verifyarr import db
from verifyarr.db import update_state


def handle_suspect(subtitle_path: Path, video_path: Path, cfg: Config, media_root: Path,
                    lang: Optional[str], bazarr_meta: Optional[dict],
                    history_index: Optional[dict], auto_action: str,
                    conn: Optional[sqlite3.Connection] = None,
                    run_id: Optional[int] = None, cancel_event=None) -> str:
    """auto_action: off | quarantine | blacklist | remediate — the CALLER decides which (see
    process_pair below): correctness_auto_action for a plain correctness SUSPECT,
    line_order_auto_action for a widespread line-order swap. Two independent settings, since the
    two checks can warrant different amounts of trust in their own SUSPECT verdict.

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
        try:
            dest = quarantine_subtitle(subtitle_path, cfg.quarantine_dir, media_root)
        except Exception as e:
            log.warning("Could not quarantine %s: %s", subtitle_path, e)
            return f"quarantine failed: {e}"
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
        try:
            dest = quarantine_subtitle(subtitle_path, cfg.quarantine_dir, media_root)
        except Exception as e:
            log.warning("Could not quarantine %s: %s", subtitle_path, e)
            return f"quarantine failed: {e}"
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
        try:
            dest = quarantine_subtitle(subtitle_path, cfg.quarantine_dir, media_root)
        except Exception as e:
            return f"blacklist failed, and could not quarantine either: {e}"
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
    row, current_subs = sync_pair(video_path, subtitle_path, lang, cfg, audio_cache, audio_cache_dir)
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


def _write_fix(subtitle_path: Path, cfg: Config, media_root: Path, source) -> None:
    """Puts a sync result on disk, backing the original up first when configured. `source` is
    either alass's own output file (a temp Path, copied verbatim) or an already-parsed
    pysubs2 file (saved via pysubs2)."""
    if cfg.backup_originals:
        backup_subtitle(subtitle_path, cfg.backup_dir, media_root)
    if isinstance(source, Path):
        shutil.copyfile(source, subtitle_path)
    else:
        source.save(str(subtitle_path))


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
    ambiguous = row.pop("_ambiguous_sync", None)
    if ambiguous is None:
        return None
    new_subs = ambiguous["new_subs"]
    _write_fix(subtitle_path, cfg, cfg.media_root_for(subtitle_path), new_subs)
    max_shift_new = ambiguous["max_shift_new"]
    row["sync_status"] = f"fixed (Δ{max_shift_new:.1f}s)"
    row["sync_max_shift_s"] = round(max_shift_new, 2) if max_shift_new is not None else None
    row["sync_split_blocks"] = 1
    row["sync_block_spread_s"] = None
    spread = ambiguous.get("blocks_spread")
    row["note"] += (f" alass also found a {ambiguous.get('blocks_split_count') or '?'}-block fit"
                    + (f" (spread {spread:.1f}s)" if spread is not None else "")
                    + f", but it couldn't be verified against the audio ({reason}) — applied the "
                    f"single-offset fit unverified.")
    return new_subs


def sync_pair(video_path: Path, subtitle_path: Path, lang: Optional[str], cfg: Config,
              audio_cache: Optional[dict] = None, audio_cache_dir: Optional[Path] = None,
              audio_cache_lock=None, defer_verification: bool = True) -> tuple[dict, object]:
    """Sync stage only (alass) — the first half of what process_pair used to do in one piece.
    No `conn`/DB access at all, which is exactly what makes it safe to run from a worker thread
    (see jobs._run_sweep's parallel sync phase — alass itself is single-threaded per invocation,
    verified against its own Cargo.toml, so running several at once is what actually uses more
    than one of the NAS's cores). correctness_and_finish below picks up from here, sequentially.

    alass runs in its normal split-penalty mode first (the only mode there was before the
    verified-second-opinion feature). When that comes back as ONE block, it already IS a single
    global offset -- nothing to second-guess, applied directly. Only a MULTI-block result is
    structurally suspicious (alass can fit mismatched content in pieces just as happily as it
    fits real cuts -- see sync_engine.parse_alass_shift_blocks), and only then is the second,
    --no-split run made: a single-offset fit that can't overfit in pieces. Both are then held
    back from disk (row["_ambiguous_sync"]) for correctness_and_finish to compare against the
    original on real audio content (_resolve_ambiguous_sync) before anything is written --
    reusing the Whisper samples the correctness check pays for anyway. That deferral only
    happens when the comparison can actually run (correctness check on, API key present, not a
    dry-run, and defer_verification -- a caller that will never run correctness_and_finish
    passes False, e.g. jobs._run_generate_single); otherwise the multi-block fit is applied
    directly, exactly as before the feature existed, and correctness_and_finish's block-spread
    safety net still watches it.

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

    alass_bin = resolve_alass_bin()
    current_subs = old_subs
    media_root = cfg.media_root_for(subtitle_path)

    if not cfg.sync_enabled:
        # Settings -> Automation "What runs" table (sync.enabled), further narrowed for an
        # auto-triggered run (the Bazarr wanted-subtitles poll) by auto_scan_sync_enabled — see
        # jobs._effective_cfg.
        row["sync_status"] = "skipped (disabled in settings)"
        return row, current_subs
    if not alass_bin:
        row["sync_status"] = "alass not found"
        return row, current_subs

    with tempfile.TemporaryDirectory() as td:
        reference_path = resolve_alass_reference(video_path, audio_cache, audio_cache_dir, audio_cache_lock)
        tmp_primary = Path(td) / f"synced{subtitle_path.suffix}"
        ok, msg, stderr_tail = run_alass(alass_bin, reference_path, subtitle_path, tmp_primary, cfg.split_penalty)
        if not ok:
            row["sync_status"] = f"error: {msg}"
            row["note"] = stderr_tail[:300]
            return row, current_subs
        try:
            primary_subs = load_subs(tmp_primary)
        except Exception as e:
            row["sync_status"] = "could not parse alass output"
            row["note"] = str(e)
            return row, current_subs

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
            row["note"] += blocks_note
            return row, current_subs

        # Use the corrected timing for the correctness check either way — even during dry-run,
        # where nothing is written to disk yet, but the report should still reflect what WOULD
        # happen. Otherwise Whisper audio gets compared against the old, wrong timing, producing
        # false SUSPECT flags on exactly the files with the biggest sync error.
        current_subs = primary_subs
        if cfg.dry_run:
            row["sync_status"] = f"would fix (Δ{max_shift:.1f}s) [dry-run]"
            row["note"] += blocks_note + structural_note
            return row, current_subs

        can_verify = (len(shift_blocks) > 1 and defer_verification
                      and cfg.enable_correctness_check and cfg.has_stt_configured)
        if can_verify:
            tmp_single = Path(td) / f"synced_single{subtitle_path.suffix}"
            ok2, _msg2, _stderr2 = run_alass(alass_bin, reference_path, subtitle_path, tmp_single,
                                             cfg.split_penalty, no_splits=True)
            single_subs = None
            if ok2:
                try:
                    single_subs = load_subs(tmp_single)
                except Exception:
                    single_subs = None
            if single_subs is not None:
                max_shift_single, *_ = max_shift_stats(old_subs, single_subs)
                # Held back from disk: correctness_and_finish decides which of {original, this
                # single-offset fit, the multi-block fit} actually scores best against the real
                # audio (see _resolve_ambiguous_sync) before anything gets written. The
                # single-offset fit is the candidate the correctness check itself runs on (the
                # safe default); the block ranges let it aim extra samples at every block.
                row["_ambiguous_sync"] = {
                    "old_subs": old_subs, "new_subs": single_subs, "max_shift_new": max_shift_single,
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
                return row, current_subs
            blocks_note += " (alass's --no-split alternative failed, so this multi-block fit was applied unverified.)"

        _write_fix(subtitle_path, cfg, media_root, tmp_primary)
        row["sync_status"] = f"fixed (Δ{max_shift:.1f}s)"
        row["note"] += blocks_note + structural_note
    return row, current_subs


# Tie-break order between sync candidates in _resolve_ambiguous_sync when the evidence can't
# separate them: alass's single-offset fit (can't overfit in pieces) over its multi-block fit
# (alass's evidence that a shift is needed, but structurally the riskier one) over leaving the
# original alone (alass measured a real offset, so "untouched" is not the neutral choice).
_CANDIDATE_PREFERENCE = {"new": 0, "blocks": 1, "old": 2}

# How close two "ok" candidates' CONTENT scores (avg_score -- fraction of matching words) have
# to be before timing/preference gets to break the tie, once content has actually been scored
# for more than one candidate. A real gap this size or bigger is a genuine "one of these
# matches the episode's actual dialogue much better" signal that anchors must not overrule --
# verified against a real file where the single-offset fit scored 0.43 against the multi-block
# fit's 0.90 (more than half its own sampled text failed to match anything) and STILL won,
# because the old code picked among "ok" candidates using the same anchor-only pick() the free
# fast-path gate uses, silently discarding the content scores it had just paid API calls for.
CONTENT_SCORE_TIE_MARGIN = 0.1


def _resolve_ambiguous_sync(conn: sqlite3.Connection, video_path: Path, subtitle_path: Path,
                             lang: Optional[str], cfg: Config, media_root: Path,
                             ambiguous: dict, result: dict, row: dict, cancel_event=None) -> tuple:
    """Decides which of {alass's --no-split single-global-offset fit ("new", the candidate the
    correctness check `result` was run on), its multi-block split-penalty fit ("blocks"), the
    ORIGINAL subtitle ("old")} to actually keep -- see sync_pair for when this arises. Every
    candidate is judged on the SAME cached Whisper samples (correctness.evaluate_against_cached_
    transcripts -- the ones the correctness check just paid for, plus any older ones for this
    video), so no new Whisper calls, and no candidate gets an easier or harder sample set than
    another.

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
    transcript_lang = result.get("audio_lang")
    subs_by_key = {"new": new_subs, "blocks": blocks_subs, "old": old_subs}
    subs_by_key = {k: v for k, v in subs_by_key.items() if v is not None}

    # 1. Timing evidence -- free.
    timing: dict[str, Optional[dict]] = {}
    for key, subs in subs_by_key.items():
        ev = evaluate_against_cached_transcripts(conn, video_path, subs, lang, transcript_lang, cfg, score=False)
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
    new_confirmed = _confirmed_in_every_block("new") if any_anchor_evidence else True
    if result.get("flag") == "ok" and pick(subs_by_key) == "new" and new_confirmed:
        winner = "new"
    else:
        for key, subs in subs_by_key.items():
            if key == "new":
                continue
            ev = evaluate_against_cached_transcripts(conn, video_path, subs, lang, transcript_lang, cfg,
                                                     score=True, cancel_event=cancel_event)
            if ev is not None:
                scored[key] = ev
        content_ok = [k for k, v in scored.items() if v.get("flag") == "ok" and v.get("avg_score") is not None]
        if content_ok:
            # CONTENT decides first among candidates that passed it -- it's the evidence this
            # branch just paid for, and a real gap between two "ok" scores is exactly what it
            # exists to catch (see CONTENT_SCORE_TIE_MARGIN). Anchors/preference (pick()) only
            # get a say among whichever candidates are within that margin of the best score --
            # a genuine tie, not a decisive difference silently thrown away.
            best = max(scored[k]["avg_score"] for k in content_ok)
            near_best = [k for k in content_ok if scored[k]["avg_score"] >= best - CONTENT_SCORE_TIE_MARGIN]
            winner = _reject_unproven_old(pick(near_best), near_best)
        else:
            scorable = [k for k, v in scored.items() if v.get("avg_score") is not None]
            winner = (max(scorable, key=lambda k: (scored[k]["avg_score"], -_CANDIDATE_PREFERENCE[k]))
                      if scorable else "new")
            winner = _reject_unproven_old(winner, scorable)

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
    note_suffix = (f" Verified alass's {n_blocks}-block fit against its single-offset fit and the original "
                   f"on the same Whisper samples before applying anything — kept '{winner}' ["
                   + "; ".join(_describe(k) for k in subs_by_key) + "].")
    structural_note = " line count changed significantly — check the file manually." if ambiguous.get("structural") else ""

    def _synthetic(key: str) -> dict:
        s = scored[key]
        return {"avg_score": s.get("avg_score"), "flag": s.get("flag"), "samples": s.get("samples") or [],
                "audio_lang": transcript_lang, "swap_severity": None}

    max_shift_new = ambiguous.get("max_shift_new")
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

    # winner == "old" -- nothing to write, the file was never touched on disk in the first place.
    # sync_max_shift_s deliberately keeps the offset alass measured: it's the one fact worth
    # seeing in the UI about a file whose re-sync was rejected.
    shift_txt = f"{max_shift_new:.1f}s" if max_shift_new is not None else "?"
    row["sync_status"] = f"already in sync (alass suggested Δ{shift_txt}, rejected — didn't verify better than the original)"
    row["sync_split_blocks"] = None
    row["sync_block_spread_s"] = None
    row["note"] += note_suffix
    row["line_order_fixed"] = None
    row["line_order_flagged"] = None
    return old_subs, _synthetic("old"), None, winner


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
    if current_subs is None:
        # sync_pair couldn't parse the original subtitle at all -- nothing to correctness-check.
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
        cache_key = cache_key_for(current_subs, cfg)
        cached = db.get_line_order_cache(conn, subtitle_path)
        reused_cache = bool(cached and cached["key"] == cache_key)
        try:
            if reused_cache:
                collected = json.loads(cached["json"])
                collected["whisper_verdicts"] = {int(k): v for k, v in collected["whisper_verdicts"].items()}
                collected["tested_items"] = [tuple(t) for t in collected["tested_items"]]
                collected["candidates"] = [tuple(c) for c in collected["candidates"]]
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
        except JobCancelled:
            raise  # a cancelled job is not a "skipped" check -- let jobs.py end the run cleanly
        except Exception as e:
            log.warning("Correctness/line-order check failed for %s: %s", subtitle_path, e)
            collected = {"skipped": True, "reason": str(e)}

        if collected.get("skipped"):
            row["correctness_flag"] = "skipped"
            row["note"] = (row["note"] + " | " + collected.get("reason", "")).strip(" |")
        else:
            if reused_cache:
                log.info("Reusing cached correctness/line-order data for %s (subtitle unchanged "
                          "since last check).", subtitle_path.name)
            else:
                row["line_order_cache_key"] = cache_key
                row["line_order_cache_json"] = json.dumps({
                    "samples": collected["samples"], "audio_lang": collected["audio_lang"],
                    "whisper_verdicts": collected["whisper_verdicts"],
                    "tested_items": collected["tested_items"], "candidates": collected["candidates"],
                })

            act_on_line_order = cfg.line_order_enabled and cfg.line_order_audio_confirm
            result = finalize_line_order(collected, cfg, cancel_event=cancel_event,
                                          run_llm_confirm=act_on_line_order)
            row["correctness_avg_score"] = round(result["avg_score"], 3) if result["avg_score"] is not None else None
            row["correctness_audio_lang"] = result.get("audio_lang")
            row["correctness_samples"] = result.get("samples")
            swap_severity = result.get("swap_severity")

            # A fix sync_pair deferred writing (a multi-block alass result held back together
            # with its single-offset alternative — see its own docstring) gets resolved HERE,
            # before any of the branches
            # below act on `result` -- the winner might not even be "new" (the candidate
            # `result` currently describes), so nothing downstream should judge/act on "new"
            # until this has had a chance to replace it.
            ambiguous = row.pop("_ambiguous_sync", None)
            resolved_winner = None
            if ambiguous is not None:
                current_subs, result, swap_severity, resolved_winner = _resolve_ambiguous_sync(
                    conn, video_path, subtitle_path, lang, cfg, media_root, ambiguous, result, row,
                    cancel_event=cancel_event)
                row["correctness_avg_score"] = round(result["avg_score"], 3) if result["avg_score"] is not None else None
                row["correctness_audio_lang"] = result.get("audio_lang")
                row["correctness_samples"] = result.get("samples")

            if swap_severity is not None:
                # Whisper AND an independent LLM pass both confirmed a large share of the TESTED
                # heuristic candidates are genuinely swapped — not a per-line fix, treat the whole
                # file as SUSPECT and let Bazarr find a better release instead.
                row["correctness_avg_score"] = swap_severity["whisper_rate"]
                row["correctness_flag"] = "SUSPECT"
                row["note"] = (row["note"] + " Line order: widespread swaps confirmed by both "
                                f"Whisper ({swap_severity['whisper_confirmed']}/{swap_severity['whisper_checked']}) "
                                f"and the LLM ({swap_severity['llm_confirmed']}/{swap_severity['llm_checked']}) on "
                                f"{swap_severity['sample_size']} tested candidate(s) — treating as SUSPECT instead "
                                "of auto-fixing individual lines.").strip()
                row["auto_action"] = handle_suspect(subtitle_path, video_path, cfg, media_root, lang,
                                                     bazarr_meta, history_index, cfg.line_order_auto_action,
                                                     conn=conn, run_id=run_id, cancel_event=cancel_event)
            elif result["flag"] == "SUSPECT":
                row["correctness_flag"] = "SUSPECT"
                excerpts = " || ".join(
                    f"{s['start']}s: \"{s.get('transcript_excerpt', s.get('error', ''))}\""
                    for s in result["samples"]
                )
                row["note"] = (row["note"] + " Whisper heard: " + excerpts).strip()
                row["auto_action"] = handle_suspect(subtitle_path, video_path, cfg, media_root, lang,
                                                     bazarr_meta, history_index, cfg.correctness_auto_action,
                                                     conn=conn, run_id=run_id, cancel_event=cancel_event)
            elif (resolved_winner is None
                  and row["sync_block_spread_s"] is not None
                  and row["sync_block_spread_s"] >= cfg.block_spread_suspect_threshold_s
                  and any(s.get("score") is not None and s["score"] < cfg.overlap_threshold
                          for s in result["samples"])):
                # A safety net for the one case that skips the {original, single-offset,
                # multi-block} comparison above entirely: sync_pair applied a multi-block fix
                # directly (its --no-split alternative failed, or the comparison couldn't run)
                # with nothing to verify it against.
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
                row["note"] = (row["note"] +
                                f" Escalated to SUSPECT: alass needed a {row['sync_block_spread_s']:.1f}s-spread "
                                f"multi-block sync AND {len(failing)}/{len(result['samples'])} Whisper sample(s) "
                                "didn't match their window on their own -- majority-vote alone wasn't enough to "
                                "trust this file.").strip()
                row["auto_action"] = handle_suspect(subtitle_path, video_path, cfg, media_root, lang,
                                                     bazarr_meta, history_index, cfg.correctness_auto_action,
                                                     conn=conn, run_id=run_id, cancel_event=cancel_event)
            elif cfg.anchor_check_enabled and (bad := significant_anchor_residuals(
                    result.get("samples") or [], ANCHOR_SUSPECT_THRESHOLD_S)):
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
                row["correctness_flag"] = "SUSPECT"
                row["note"] = (row["note"] +
                                f" Escalated to SUSPECT: {len(bad)} Whisper anchor(s) show a confirmed "
                                f"timing mismatch of up to {worst:.1f}s at [{where}], even though "
                                "the overall average passed.").strip()
                row["auto_action"] = handle_suspect(subtitle_path, video_path, cfg, media_root, lang,
                                                     bazarr_meta, history_index, cfg.correctness_auto_action,
                                                     conn=conn, run_id=run_id, cancel_event=cancel_event)
            else:
                row["correctness_flag"] = "ok"
                if act_on_line_order:
                    issues = result.get("line_issues") or []
                    flagged = result.get("line_flagged") or []
                    row["line_order_fixed"] = len(issues)
                    row["line_order_flagged"] = len(flagged)
                    if issues:
                        if cfg.dry_run:
                            row["note"] += f" Line order: would auto-fix {len(issues)} block(s) [dry-run]."
                        else:
                            if cfg.backup_originals:
                                backup_subtitle(subtitle_path, cfg.backup_dir, media_root)
                            for item in issues:
                                apply_line_swap(current_subs, item["index"])
                            current_subs.save(str(subtitle_path))
                            row["note"] += f" Line order: auto-fixed {len(issues)} block(s)."
                    if flagged:
                        row["note"] += (f" Line order: {len(flagged)} block(s) flagged for manual "
                                         f"review (not auto-fixed, unconfirmed).")
                elif cfg.line_order_enabled:
                    # audio_confirm off — heuristic-only reporting, never auto-fixed, same low-
                    # confidence handling as before, just sourced from the candidates already
                    # collected above instead of a separate pass.
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
    update_state(conn, video_path, subtitle_path, row, run_id=run_id, media_root=media_root)
    return row
