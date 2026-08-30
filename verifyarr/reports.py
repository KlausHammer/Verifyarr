"""JSONL reports — one line per processed file, written at the end of each run. Kept as a
power-user facility (`/data/reports/`) independent of the webapp's own database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from verifyarr import log


def write_report(rows: list[dict], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = report_dir / f"report-{ts}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    suspects = [r for r in rows if r["correctness_flag"] == "SUSPECT"]
    changed = [r for r in rows if r["sync_status"].startswith("fixed")]
    errors = [r for r in rows if "error" in r["sync_status"]]

    log.info("=== Run finished: %d files processed ===", len(rows))
    log.info("Sync fixed: %d | Already fine: %d | Errors: %d",
              len(changed), sum(1 for r in rows if r["sync_status"] == "already in sync"), len(errors))
    if suspects:
        log.warning("--- %d SUSPECT file(s) ---", len(suspects))
        for r in suspects:
            log.warning("  %s  (score=%s) -> %s", r["subtitle"], r["correctness_avg_score"], r["auto_action"])
    log.info("Full report: %s", out_path)
    return out_path
