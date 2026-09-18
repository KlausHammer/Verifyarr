"""Parallel launcher for e2e_matrix.py: one process per (episode, model).

Each shard gets its own --shard id, hence its own DB, workdir and output
file, so there is no shared state. After all shards finish, the per-shard
jsonl files are merged into <out>.jsonl + <out>_summary.json (same shape as
a serial run -- corruptions are seeded per slug+scenario and sweep loading
is deterministic, so sharding changes nothing). Resume-safe: e2e_matrix.py
itself skips completed rows, so re-running this after an interruption only
does the missing work (skipped rows are retried, never counted as done).

Sharding is the product of episodes and models (10 x 15 = 150 shards), so
all 16 cores stay busy: each shard runs one model on one episode
(--models <one> --only <one>, 24 rows). A per-episode sharding would leave
a long tail of slow episodes on few workers.

Run: .venv/bin/python tests/e2e_matrix_parallel.py [--workers 16]
       [--models turbo,small.en-q5_1] [--only C_S02E02,...]
       [--scenarios clean,uniform,...] [--mode full|sampled]
       [--audio-confirm on|off] [--out e2e_matrix] [--redo]
"""
from __future__ import annotations
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
STAGING = HERE.parent
PY = STAGING / ".venv" / "bin" / "python"

sys.path.insert(0, str(HERE))
import e2e_matrix as M  # noqa: E402  (SLUGS/ALL_MODELS/build_summary live there)


def build_shards(slugs, models):
    """Cartesian product of episodes and models, slug-major order."""
    return [(slug, model) for slug in slugs for model in models]


def run_shard(args):
    i, slug, model, extra = args
    log = HERE / f"e2e_work_matrix_{i}.log"
    cmd = [str(PY), str(HERE / "e2e_matrix.py"),
           "--only", slug, "--models", model, "--shard", str(i)] + extra
    with open(log, "w", encoding="utf-8") as fh:
        p = subprocess.run(cmd, cwd=str(STAGING), stdout=fh, stderr=subprocess.STDOUT)
    tail = log.read_text(encoding="utf-8").strip().splitlines()[-3:]
    return slug, model, p.returncode, tail


def main():
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    slugs = (sys.argv[sys.argv.index("--only") + 1].split(",")
             if "--only" in sys.argv else list(M.SLUGS))
    models = (sys.argv[sys.argv.index("--models") + 1].split(",")
              if "--models" in sys.argv else list(M.ALL_MODELS))
    out_stem = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "e2e_matrix"
    extra = []
    for flag in ("--scenarios", "--mode", "--audio-confirm", "--out", "--redo",
                 "--escalate-min-bad", "--no-escalate", "--suspect-min",
                 "--escalate-any-block", "--escalate-on"):
        if flag in sys.argv:
            if flag in ("--redo", "--no-escalate", "--escalate-any-block", "--escalate-on"):
                extra.append(flag)
            else:
                extra += [flag, sys.argv[sys.argv.index(flag) + 1]]
    scen_order = (sys.argv[sys.argv.index("--scenarios") + 1].split(",")
                  if "--scenarios" in sys.argv else list(M.DEFAULT_SCENARIOS))
    mode_order = (sys.argv[sys.argv.index("--mode") + 1].split(",")
                  if "--mode" in sys.argv else list(M.MODES))
    audio_order = (sys.argv[sys.argv.index("--audio-confirm") + 1].split(",")
                   if "--audio-confirm" in sys.argv else list(M.AUDIOS))
    shards = build_shards(slugs, models)
    print(f"shards={len(shards)} workers={workers} out={out_stem}", flush=True)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        jobs = [(i, slug, model, extra) for i, (slug, model) in enumerate(shards)]
        for slug, model, rc, tail in ex.map(run_shard, jobs):
            print(f"[{slug}.{model}] rc={rc}", flush=True)
            for line in tail:
                print(f"[{slug}.{model}] {line}", flush=True)
    rows = []
    for i, (slug, model) in enumerate(shards):
        p = HERE / f"{out_stem}_{i}.jsonl"
        if not p.exists():
            print(f"[{slug}.{model}] no output file, skipped", flush=True)
            continue
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: (slugs.index(r["slug"]) if r["slug"] in slugs else 99,
                             models.index(r["model"]) if r["model"] in models else 99,
                             scen_order.index(r["scenario"]) if r["scenario"] in scen_order else 99,
                             mode_order.index(r.get("mode", "full"))
                             if r.get("mode", "full") in mode_order else 99,
                             audio_order.index(r.get("audio_confirm", "on"))
                             if r.get("audio_confirm", "on") in audio_order else 99))
    rows = M.dedupe_rows(rows)
    (HERE / f"{out_stem}.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary = M.build_summary(rows, models, slugs, scen_order, mode_order, audio_order)
    (HERE / f"{out_stem}_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("merged", len(rows), "rows;", json.dumps(summary["by_scenario"], indent=1))


if __name__ == "__main__":
    main()
