"""Turns a Known Good matrix run into the numbers the README and docs quote.

    python tests/known_good/analyze.py tests/known_good/results/matrix.jsonl.gz [--out tests/known_good/results]

Writes summary.json and tables.md next to the results. Pass rules (the project's two arms):
  * offset, rate         fixed: recovered p50 <= 0.25 s and >= 98 % of lines within 1 s of the truth
  * blocks               detected: the file is flagged (flag != ok); repair is a bonus, reported separately
  * holes (missing middle, random holes)   detected: flagged AND the file is left untouched. Only holes that
                         removed >= 20 dialogue lines count in the headline; smaller ones are reported on their
                         own ("small_holes"), because a hole that removed 0-9 lines of dialogue is not a missing scene
  * cut-off start/end    reported on its own ("cut_ends"): the first and last two minutes are deliberately not judged
                         (songs, recaps, promos), so a cut that short cannot be seen
  * offset +0.3 s        reported on its own ("offset_boundary"): it sits just above the 0.25 s decision bar
  * wrong episode        flagged SUSPECT and left untouched
  * swapped lines        noticed: flagged for review (nothing is auto-fixed without audio confirmation)
  * drift + swap         timing fixed AND the swap noticed
  * dropped/duplicated cues   left untouched
  * per-line jitter      no worse than injected (nothing systematic to fix)
  * clean                left untouched and not flagged
The truth is the user-verified original subtitle of every episode (all Known Good episodes are verified correct).
"""
import collections
import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

OFFSET = ["uniform", "uniform_neg", "uniform_m07", "uniform_p15", "uniform_m5"]
BOUNDARY = ["uniform_p03"]
HOLE_MIN_DIALOGUE = 20
RATE = (["fps_late", "fps_early", "drift", "drift_offset", "pal_late", "pal_early"]
        + [f"drift_rand{i}" for i in range(6)] + [f"ratio_rand{i}" for i in range(4)])
SLOW = [f"slow_rand{i}" for i in range(10)]   # 0.03-0.15 % ramps: the weak-evidence regime
BLOCKS = (["piecewise", "piecewise_b", "piecewise_c", "cut_version"]
          + [f"block_rand{i}" for i in range(4)] + [f"blocks_rand{i}" for i in range(2)]
          + [f"cutsteps_rand{i}" for i in range(10)])
HOLES = ["missing_middle"] + [f"hole_rand{i}" for i in range(4)]
CUT_ENDS = [f"trunc_start_rand{i}" for i in range(2)] + [f"trunc_end_rand{i}" for i in range(2)]
GROUPS = {
    "offset": OFFSET, "rate": RATE, "slow_rate": SLOW, "blocks": BLOCKS, "holes": HOLES,
    "small_holes": HOLES, "cut_ends": CUT_ENDS, "offset_boundary": BOUNDARY,
    "wrong_episode": ["wrong_episode"], "swap": ["swap", "many_swaps"], "drift_swap": ["drift_swap"],
    "dropdup": ["dropdup"], "jitter": ["jitter"], "clean": ["clean"],
}
GROUP_LABEL = {
    "offset": "Constant offset (0.7 s to 45 s)", "offset_boundary": "Offset +0.3 s (boundary)",
    "small_holes": "Small holes (< 20 dialogue lines)",
    "cut_ends": "Start or end cut off (first/last 2 min are not judged)", "rate": "Framerate, PAL, drift (rate errors)", "slow_rate": "Weak drift 0.03-0.15 % (+ up to 3 s offset)",
    "blocks": "Blocks at different offsets", "holes": "Missing stretch (>= 20 dialogue lines)",
    "wrong_episode": "Wrong episode", "swap": "Swapped lines", "drift_swap": "Drift + swapped lines",
    "dropdup": "Dropped / duplicated cues", "jitter": "Per-line jitter (nothing to fix)",
    "clean": "Healthy file (no false alarm)",
}


def timing_pass(r):
    rec = r.get("recovered") or {}
    return rec.get("p50") is not None and rec["p50"] <= 0.25 and (rec.get("frac_le_1_0s") or 0) >= 0.98


def jitter_pass(r):
    rec = r.get("recovered") or {}
    inj = r.get("injected_p50")
    return rec.get("p50") is not None and inj is not None and rec["p50"] <= inj


PASS = {}
for s in OFFSET + BOUNDARY + RATE + SLOW:
    PASS[s] = timing_pass
for s in BLOCKS:
    PASS[s] = lambda r: (r.get("flag") or "ok") != "ok"
for s in HOLES + CUT_ENDS:
    PASS[s] = lambda r: bool(r.get("detected"))
PASS["wrong_episode"] = lambda r: r.get("flag") == "SUSPECT" and bool(r.get("untouched"))
PASS["swap"] = PASS["many_swaps"] = lambda r: bool(r.get("swap_noticed"))
PASS["drift_swap"] = lambda r: timing_pass(r) and bool(r.get("swap_noticed"))
PASS["dropdup"] = lambda r: bool(r.get("untouched"))
PASS["jitter"] = jitter_pass
PASS["clean"] = lambda r: bool(r.get("untouched")) and (r.get("flag") or "ok") == "ok"
GROUP_OF = {s: g for g, ss in GROUPS.items() if g != "small_holes" for s in ss}


def group_of(r):
    """The report group of a result row (small holes are split off the hole scenarios)."""
    g = GROUP_OF[r["scenario"]]
    if g == "holes":
        rd = (r.get("detail") or {}).get("removed_dialogue")
        if rd is not None and rd < HOLE_MIN_DIALOGUE:
            return "small_holes"
    return g
NEEDS_RECOVERED = set(OFFSET + RATE + SLOW + ["drift_swap", "jitter"])


def load(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(rows):
    ok = [r for r in rows if r.get("status", "ok") == "ok" and r["scenario"] in PASS]
    models = sorted({r["model"] for r in ok})
    modes = sorted({r["mode"] for r in ok})
    cell = collections.defaultdict(lambda: [0, 0])          # (model, mode, group) -> [passed, total]
    fails = collections.defaultdict(list)
    for r in ok:
        if r["scenario"] in NEEDS_RECOVERED and "recovered" not in r:
            continue
        key = (r["model"], r["mode"], group_of(r))
        cell[key][1] += 1
        if PASS[r["scenario"]](r):
            cell[key][0] += 1
        else:
            fails[key].append(f'{r["slug"]}:{r["scenario"]}')
    hole_bins = collections.defaultdict(lambda: [0, 0])     # detection by how much dialogue the hole removed
    for r in ok:
        if r["scenario"] in HOLES and (r.get("detail") or {}).get("removed_dialogue") is not None:
            rd = r["detail"]["removed_dialogue"]
            b = "0-9" if rd < 10 else "10-19" if rd < 20 else "20-39" if rd < 40 else "40+"
            hole_bins[b][1] += 1
            hole_bins[b][0] += bool(r.get("detected"))
    cut_bins = collections.defaultdict(lambda: [0, 0])      # cut-off start/end by the length that was cut
    for r in ok:
        if r["scenario"] in CUT_ENDS:
            rs = (r.get("detail") or {}).get("removed_s") or 0
            b = "< 150 s" if rs < 150 else "150-250 s" if rs < 250 else ">= 250 s"
            cut_bins[b][1] += 1
            cut_bins[b][0] += bool(r.get("detected"))
    by_scenario = collections.defaultdict(lambda: [0, 0])
    for r in ok:
        if r["scenario"] in NEEDS_RECOVERED and "recovered" not in r:
            continue
        k = (r["mode"], r["scenario"])
        by_scenario[k][1] += 1
        by_scenario[k][0] += bool(PASS[r["scenario"]](r))
    repaired = collections.defaultdict(lambda: [0, 0])      # blocks that were also fixed
    for r in ok:
        if r["scenario"] in BLOCKS:
            k = (r["model"], r["mode"])
            repaired[k][1] += 1
            rec = r.get("recovered") or {}
            repaired[k][0] += bool(rec.get("p50") is not None and rec["p50"] <= 0.15 and (rec.get("frac_le_0_5s") or 0) >= 0.98)
    clean_fp = [dict(model=r["model"], mode=r["mode"], slug=r["slug"], flag=r.get("flag"), reason=r.get("reason"),
                     sync=r.get("sync")) for r in ok if r["scenario"] == "clean" and not PASS["clean"](r)]
    flagged_lines = collections.defaultdict(list)           # swap candidates on the verified-good files
    for r in ok:
        if r["scenario"] == "clean":
            flagged_lines[r["slug"]].append(len(r.get("lo_flagged_indices") or []))
    return dict(models=models, modes=modes, cell={f"{k[0]}|{k[1]}|{k[2]}": v for k, v in cell.items()},
                fails={f"{k[0]}|{k[1]}|{k[2]}": v for k, v in fails.items()},
                by_scenario={f"{k[0]}|{k[1]}": v for k, v in by_scenario.items()},
                blocks_also_repaired={f"{k[0]}|{k[1]}": v for k, v in repaired.items()},
                hole_detection_by_removed_dialogue_lines=dict(hole_bins),
                cut_end_detection_by_cut_length=dict(cut_bins),
                clean_false_positives=clean_fp,
                clean_swap_candidates={s: sorted(v) for s, v in flagged_lines.items()},
                rows=len(rows), ok_rows=len(ok))


def tables(sm):
    out = []
    for mode in sm["modes"]:
        out.append(f"\n### {mode}: passed / run per error type\n")
        out.append("| model | " + " | ".join(GROUP_LABEL[g] for g in GROUPS) + " |")
        out.append("|---|" + "---|" * len(GROUPS))
        for m in sm["models"]:
            cells = []
            for g in GROUPS:
                p, n = sm["cell"].get(f"{m}|{mode}|{g}", [0, 0])
                cells.append(f"{p}/{n}" if p == n else f"**{p}/{n}**")
            out.append(f"| {m} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main(argv):
    path = Path(argv[1])
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else path.parent
    rows = load(path)
    sm = summarize(rows)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(sm, indent=1), encoding="utf-8")
    (out / "tables.md").write_text(tables(sm), encoding="utf-8")
    print(f"{sm['ok_rows']} ok rows of {sm['rows']}; models {len(sm['models'])}; clean false positives: {len(sm['clean_false_positives'])}")
    print(tables(sm))


if __name__ == "__main__":
    main(sys.argv)
