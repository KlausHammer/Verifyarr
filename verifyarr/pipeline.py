"""Core per-file flow — process_pair runs sync (alass) + correctness check (Whisper) for
one video/subtitle pair, and handle_suspect deals with the result if it gets flagged
SUSPECT. Called from both sweep/single (verifyarr.cli) and the webapp's job runner."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs, max_shift_stats
from verifyarr.sync_engine import resolve_alass_bin, resolve_alass_reference, run_alass, parse_alass_shift_blocks
from verifyarr.line_order import heuristic_candidates, collect_samples, finalize_line_order, cache_key_for, apply_line_swap
from verifyarr.fileops import backup_subtitle, quarantine_subtitle
from verifyarr.bazarr import bazarr_map_path, bazarr_blacklist, remediate_suspect
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
        # No Bazarr match -- Bazarr can't remove/replace it for us, so fall back to a plain
        # local quarantine move instead, same as quarantine mode.
        try:
            dest = quarantine_subtitle(subtitle_path, cfg.quarantine_dir, media_root)
        except Exception as e:
            log.warning("Could not quarantine %s: %s", subtitle_path, e)
            return f"quarantine failed: {e}"
        return f"quarantined -> {dest}; blacklist skipped (no Bazarr match)"

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
            return f"blacklist failed; quarantined -> {dest} instead"
        except Exception as e:
            return f"blacklist failed, and could not quarantine either: {e}"

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
    row = {
        "video": str(video_path), "subtitle": str(subtitle_path), "lang": lang or "",
        "sync_status": "-", "sync_max_shift_s": None, "structural_change": False,
        "sync_split_blocks": None,
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
        update_state(conn, video_path, subtitle_path, row)
        return row

    alass_bin = resolve_alass_bin()
    current_subs = old_subs
    media_root = cfg.media_root_for(subtitle_path)

    if not cfg.sync_enabled:
        # Settings -> Automation "What runs" table (sync.enabled), further narrowed for an
        # auto-triggered run (the Bazarr wanted-subtitles poll) by auto_scan_sync_enabled — see
        # jobs._effective_cfg.
        row["sync_status"] = "skipped (disabled in settings)"
    elif not alass_bin:
        row["sync_status"] = "alass not found"
    else:
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td) / f"synced{subtitle_path.suffix}"
            reference_path = resolve_alass_reference(video_path, audio_cache, audio_cache_dir)
            ok, msg, stderr_tail = run_alass(alass_bin, reference_path, subtitle_path, tmp_out, cfg.split_penalty)
            if not ok:
                row["sync_status"] = f"error: {msg}"
                row["note"] = stderr_tail[:300]
            else:
                try:
                    new_subs = load_subs(tmp_out)
                except Exception as e:
                    row["sync_status"] = "could not parse alass output"
                    row["note"] = str(e)
                else:
                    max_shift, _avg_shift, old_n, new_n = max_shift_stats(old_subs, new_subs)
                    row["sync_max_shift_s"] = round(max_shift, 2) if max_shift is not None else None
                    structural = bool(old_n) and abs(old_n - new_n) / old_n > 0.1
                    row["structural_change"] = structural

                    shift_blocks = parse_alass_shift_blocks(stderr_tail)
                    row["sync_split_blocks"] = len(shift_blocks)
                    if len(shift_blocks) > 1:
                        spread = max(shift_blocks) - min(shift_blocks)
                        row["note"] += (f" alass used {len(shift_blocks)} sync blocks with shifts "
                                         f"{[round(s, 1) for s in shift_blocks]}s (spread "
                                         f"{spread:.1f}s) — can be caused by real cuts in the episode, "
                                         f"but can also be a sign of a wrong subtitle, check manually.")

                    if max_shift is not None and max_shift >= cfg.min_change_seconds:
                        # Use the corrected timing for the correctness check either way — even
                        # during dry-run, where nothing is written to disk yet, but the report
                        # should still reflect what WOULD happen. Otherwise Whisper audio gets
                        # compared against the old, wrong timing, producing false SUSPECT flags
                        # on exactly the files with the biggest sync error.
                        current_subs = new_subs
                        if cfg.dry_run:
                            row["sync_status"] = f"would fix (Δ{max_shift:.1f}s) [dry-run]"
                        else:
                            if cfg.backup_originals:
                                backup_subtitle(subtitle_path, cfg.backup_dir, media_root)
                            shutil.copyfile(tmp_out, subtitle_path)
                            row["sync_status"] = f"fixed (Δ{max_shift:.1f}s)"
                        if structural:
                            row["note"] += " line count changed significantly — check the file manually."
                    else:
                        row["sync_status"] = "already in sync"

    # Whether a Whisper-based correctness check can run at all this file, independent of whether
    # line-order is turned on.
    correctness_unavailable_flag = None
    if not cfg.enable_correctness_check:
        correctness_unavailable_flag = "disabled"
    elif not cfg.active_stt_api_key:
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
                with tempfile.TemporaryDirectory() as td2:
                    collected = collect_samples(video_path, current_subs, lang, cfg, Path(td2),
                                                 cancel_event=cancel_event)
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

    update_state(conn, video_path, subtitle_path, row, run_id=run_id, media_root=media_root)
    return row
