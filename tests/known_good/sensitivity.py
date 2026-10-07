"""How knife-edge is each decision constant? Scales it by 0.8 and 1.25, replays the Known Good
matrix (one model, full mode) and counts the cells whose outcome changes. A constant that flips
many cells for a 25 % nudge sits where real data piles up: a small difference in the transcript
(GPU vs CPU, another model) will flip it too.

  VERIFYARR_KG=replay python tests/known_good/sensitivity.py --out sens.jsonl [--workers 10]
Resumable: finished (constant, factor) pairs in --out are skipped."""
from __future__ import annotations
import argparse
import collections
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import analyze as A  # noqa: E402

SKIP = re.compile(r"INTERVAL|SCHEMA|PAD_SECONDS|MAX_CLIP|FULL_MODE|MAX_FULL|SAMPLED_MIN|MIN_CUE_DURATION|"
                  r"RATE_SNAP_RATIOS|FPS_RATIOS|REASONS|NOTE|^FPS_BINS$|^FPS_LOO_PARTS$")
FILES = ["subtitles.py", "pipeline.py", "correctness.py", "line_order.py"]


def constants() -> list[str]:
    names = []
    for f in FILES:
        for line in (ROOT / "verifyarr" / f).read_text().splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]*) *= *-?[0-9.]+ *(#.*)?$", line)
            if m and not SKIP.search(m.group(1)) and m.group(1) not in names:
                names.append(m.group(1))
    return names


def signature(r) -> tuple:
    return (r.get("flag") or "ok", str(r.get("sync") or "")[:18], bool(A.PASS[r["scenario"]](r)))


def load_rows(path: Path) -> dict:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("status") == "ok" and r["scenario"] in A.PASS:
                rows[(r["slug"], r["scenario"])] = r
    return rows


def run(tag: str, perturb: str, workers: int, model: str, scen_file: str) -> dict:
    out = ROOT / f"kg_sens_{tag}"
    env = {**os.environ, "VERIFYARR_KG": "replay", "VERIFYARR_PERTURB": perturb}
    scen = (HERE / scen_file).read_text().strip()
    cmd = ["nice", "-n", "19", "taskset", "-c", "1-15", sys.executable, str(ROOT / "tests" / "e2e_matrix_parallel.py"),
           "--workers", str(workers), "--models", model, "--scenarios", scen, "--mode", "full",
           "--audio-confirm", "off", "--out", str(out), "--redo"]
    subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    rows = load_rows(Path(str(out) + ".jsonl"))
    for p in [*ROOT.glob(f"kg_sens_{tag}*"), *ROOT.glob("tests/e2e_work_matrix_*")]:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    return rows


def compare(base: dict, rows: dict) -> dict:
    gained = lost = changed = 0
    by_group = collections.Counter()
    for k, b in base.items():
        r = rows.get(k)
        if r is None:
            continue
        sb, sr = signature(b), signature(r)
        if sb != sr:
            changed += 1
            by_group[A.group_of(b)] += 1
        if sb[2] != sr[2]:
            gained += sr[2]
            lost += sb[2]
    return {"cells": len(base), "changed": changed, "pass_gained": gained, "pass_lost": lost,
            "by_group": dict(by_group)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sens.jsonl")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--model", default="tiny.en-greedy-cpu")
    ap.add_argument("--factors", default="0.8,1.25")
    ap.add_argument("--only", default="")
    ap.add_argument("--match", default="", help="regex: only constants whose name matches")
    ap.add_argument("--scenarios-file", default="scenarios.txt", help="in tests/known_good/")
    args = ap.parse_args()
    out = Path(args.out)
    done = {(j["const"], j["factor"]) for j in map(json.loads, out.read_text().splitlines())} if out.exists() else set()
    base = run("base", "", args.workers, args.model, args.scenarios_file)
    print(f"baseline cells: {len(base)}", flush=True)
    names = [n for n in constants() if (not args.only or n in args.only.split(",")) and re.search(args.match, n)]
    for name in names:
        for f in map(float, args.factors.split(",")):
            if (name, f) in done:
                continue
            res = compare(base, run(f"{name}_{f}", f"{name}={f}", args.workers, args.model, args.scenarios_file))
            row = {"const": name, "factor": f, **res}
            with out.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{name} x{f}: changed {res['changed']}/{res['cells']} +{res['pass_gained']} -{res['pass_lost']}", flush=True)


if __name__ == "__main__":
    main()
