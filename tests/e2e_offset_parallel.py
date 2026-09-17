"""Parallel launcher for e2e_offset.py: one process per episode (default 8).

Each shard gets its own --shard id, hence its own DB, workdir and output file,
so there is no shared state. After all shards finish, the per-shard jsonl files
are merged into e2e_offset.jsonl + e2e_offset_summary.json (same shape as a
serial run -- corruptions are seeded per slug+scenario, so sharding changes
nothing).

Run: .venv/bin/python verifyarr_handoff/e2e_offset_parallel.py [--workers 8]
"""
from __future__ import annotations
import json
import statistics
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
STAGING = HERE.parent
PY = STAGING / ".venv" / "bin" / "python"
SLUGS = ["C_S02E01", "C_S02E05", "C_S02E09", "C_S02E12",
         "C_S03E04", "C_S03E09", "C_S03E16", "SH_S01E02"]
SCENARIOS = ["piecewise", "drift", "gap"]


def run_shard(args):
    i, slug = args
    log = HERE / f"e2e_work_offset_{i}.log"
    with open(log, "w", encoding="utf-8") as fh:
        p = subprocess.run(
            [str(PY), str(HERE / "e2e_offset.py"),
             "--only", slug, "--shard", str(i)],
            cwd=str(STAGING), capture_output=False, stdout=fh, stderr=subprocess.STDOUT)
    tail = log.read_text(encoding="utf-8").strip().splitlines()[-3:]
    return slug, p.returncode, tail


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 8
    print(f"shards={len(SLUGS)} workers={workers}", flush=True)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for slug, rc, tail in ex.map(run_shard, enumerate(SLUGS)):
            print(f"[{slug}] rc={rc}", flush=True)
            for line in tail:
                print(f"[{slug}] {line}", flush=True)
    rows = []
    for i in range(len(SLUGS)):
        p = HERE / f"e2e_offset_{i}.jsonl"
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: (SLUGS.index(r["slug"]), SCENARIOS.index(r["scenario"])))
    (HERE / "e2e_offset.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = {}
    for name in SCENARIOS:
        ok = [r for r in rows if r["scenario"] == name and "recovered" in r]
        summary[name] = {
            "runs": len(ok),
            "errors": sum(1 for r in rows if r["scenario"] == name and "error" in r),
            "mean_p50": round(statistics.mean([r["recovered"]["p50"] for r in ok]), 3) if ok else None,
            "mean_frac_le_1s": round(statistics.mean(
                [r["recovered"]["frac_le_1_0s"] for r in ok]), 3) if ok else None,
        }
    (HERE / "e2e_offset_summary.json").write_text(
        json.dumps({"slugs": SLUGS, "summary": summary}, indent=1), encoding="utf-8")
    print("merged", len(rows), "rows;", json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
