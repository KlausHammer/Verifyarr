"""JSONL reports — one line per processed file, written at the end of each run. Kept as a
power-user facility (`/data/reports/`) independent of the webapp's own database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from verifyarr import log

# One file per RUN (not per episode) -- write_report is called once with all of a run's rows,
# so a single sweep of even 10,000 episodes is still just one file. What genuinely grows
# unboundedly over time is the NUMBER of runs: a scheduled weekly sweep, and especially each
# Bazarr-triggered single-file run (one per newly downloaded episode -- see jobs._run_single),
# each get their own report file forever with no cleanup. Capped here to the most recent
# MAX_REPORTS instead of a settings knob -- this is explicitly a power-user/debugging facility
# (the webapp's own database is the real source of truth), not worth a UI control for.
MAX_REPORTS = 200


def _prune_old_reports(report_dir: Path, keep: int = MAX_REPORTS) -> None:
    reports = sorted(report_dir.glob("report-*.jsonl"))  # filename timestamp sorts chronologically
    for old in reports[:-keep] if keep > 0 else reports:
        try:
            old.unlink()
        except OSError as e:
            log.warning("Could not remove old report %s: %s", old, e)


def write_report(rows: list[dict], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = report_dir / f"report-{ts}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _prune_old_reports(report_dir)

    suspects = [r for r in rows if r["correctness_flag"] == "SUSPECT"]
    changed = [r for r in rows if r["sync_status"].startswith("fixed")]
    errors = [r for r in rows if "error" in r["sync_status"]]

    log.info("=== Run finished: %d files processed ===", len(rows))
    log.info("Sync fixed: %d | Already fine: %d | Errors: %d",
              len(changed), sum(1 for r in rows if r["sync_status"] == "already in sync"), len(errors))
    if suspects:
        log.warning("--- %d SUSPECT file(s) ---", len(suspects))
        for r in suspects:
            # note says WHY: "Whisper heard: ..." (subtitle doesn't match this episode's audio)
            # vs "Line order: ..." (widespread swapped lines instead) -- same score/flag shape,
            # different problem, so it's worth showing even truncated.
            why = (r.get("note") or "").strip()
            why = f" [{why[:150]}{'…' if len(why) > 150 else ''}]" if why else ""
            log.warning("  %s  (score=%s) -> %s%s", r["subtitle"], r["correctness_avg_score"], r["auto_action"], why)
    log.info("Full report: %s", out_path)
    return out_path
