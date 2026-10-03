"""Writes docs/img/*.svg for the README and docs/models_and_sweeps.md.
Data: tests/known_good/results/{summary.json,model_quality.json} and tests/known_good/data/speed/ (nothing is typed in by
hand except the separate music test at the bottom). Run: python3 docs/make_charts.py"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).parent / "img"
KG = Path(__file__).resolve().parent.parent / "tests" / "known_good"
CSS = """
  .bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.grid{stroke:#e3e2dc}.track{fill:#eceae4}
  .hi{fill:#2a78d6}.lo{fill:#8d8c86}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19}.t1{fill:#ffffff}.t2{fill:#c3c2b7}.grid{stroke:#3a3a37}.track{fill:#2b2b29}
    .hi{fill:#3987e5}.lo{fill:#8d8c86}}
  text{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
"""


def svg(w, h, body, title, desc):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-labelledby="t d"><title id="t">{title}</title><desc id="d">{desc}</desc>'
            f'<style>{CSS}</style><rect class="bg" width="{w}" height="{h}" rx="8"/>{body}</svg>')


def bars_panel(title, subtitle, rows, maxv, fmt, foot, width=860, x0=250, pw=380, hi_first=False):
    """Generic horizontal bar chart: rows = [(label, value, highlight, note)]."""
    H = 78 + 28 * len(rows) + 34
    b = [f'<text class="t1" x="20" y="28" font-size="16" font-weight="600">{title}</text>',
         f'<text class="t2" x="20" y="48" font-size="12.5">{subtitle}</text>']
    for j, (name, v, hi, note) in enumerate(rows):
        y = 70 + 28 * j
        w = ' font-weight="600"' if hi else ""
        b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5"{w}>{name}</text>')
        b.append(f'<rect class="track" x="{x0}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
        b.append(f'<rect class="{"hi" if hi else "lo"}" x="{x0}" y="{y + 3}" width="{pw * v / maxv:.1f}" height="16" rx="3"/>')
        b.append(f'<text class="t1" x="{x0 + pw + 8}" y="{y + 16}" font-size="12">{fmt(v)}</text>')
        if note:
            b.append(f'<text class="t2" x="{x0 + pw + 62}" y="{y + 16}" font-size="11.5">{note}</text>')
    b.append(f'<text class="t2" x="20" y="{H - 12}" font-size="11.5">{foot}</text>')
    return svg(width, H, "".join(b), title, subtitle)



ORDER = ["tiny.en-greedy-cpu", "tiny.en-cpu", "tiny.en-q5_1-cpu", "base.en-greedy-cpu", "base.en-cpu", "base.en-q5_1-cpu",
         "small.en-greedy", "small.en", "small.en-q5_1", "medium.en-greedy", "medium.en", "medium.en-q5_0",
         "turbo-q8_0", "turbo-q5_0", "groq-turbo"]
NAME = {"tiny.en-greedy-cpu": "tiny.en (greedy) — default", "tiny.en-cpu": "tiny.en", "tiny.en-q5_1-cpu": "tiny.en q5_1",
        "base.en-greedy-cpu": "base.en (greedy)", "base.en-cpu": "base.en", "base.en-q5_1-cpu": "base.en q5_1",
        "small.en-greedy": "small.en (greedy)", "small.en": "small.en", "small.en-q5_1": "small.en q5_1",
        "medium.en-greedy": "medium.en (greedy)", "medium.en": "medium.en", "medium.en-q5_0": "medium.en q5_0",
        "turbo-q8_0": "large-v3-turbo q8_0", "turbo-q5_0": "large-v3-turbo q5_0", "groq-turbo": "cloud: Groq large-v3-turbo"}
DEFAULT = "tiny.en-greedy-cpu"
HEADLINE = ["offset", "rate", "blocks", "holes", "wrong_episode", "swap", "drift_swap", "dropdup", "jitter"]
GROUP_NAME = {"offset": "Constant offset (0.7 s to 45 s)", "rate": "Framerate, PAL, drift", "blocks": "Blocks at different offsets",
              "holes": "Missing stretch (>= 20 dialogue lines)", "wrong_episode": "Wrong episode", "swap": "Swapped lines",
              "drift_swap": "Drift + swapped lines", "dropdup": "Dropped / duplicated cues",
              "jitter": "Per-line jitter (nothing to fix)", "clean": "Healthy file (no false alarm)"}


def load():
    sm = json.loads((KG / "results" / "summary.json").read_text(encoding="utf-8"))
    mq = json.loads((KG / "results" / "model_quality.json").read_text(encoding="utf-8"))
    speed = defaultdict(list)
    ram = defaultdict(float)
    with open(KG / "data" / "speed" / "cpu_alone_5min_excerpts.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["rc"] == "0":
                speed[r["model"]].append(float(r["realtidsfaktor"]))
                ram[r["model"]] = max(ram[r["model"]], float(r["maks_ram_mb"]))
    return sm, mq, {m: statistics.mean(v) for m, v in speed.items()}, dict(ram)


def cell(sm, model, mode, group):
    return sm["cell"].get(f"{model}|{mode}|{group}", [0, 0])


def share(sm, model, mode):
    p = n = 0
    for g in HEADLINE:
        a, b = cell(sm, model, mode, g)
        p += a
        n += b
    return p, n

RULE = {"offset": "fixed (median error <= 0.25 s, 98 % of lines within 1 s)", "rate": "fixed, same bar",
        "blocks": "flagged (repair is a bonus)", "holes": "flagged and left untouched",
        "wrong_episode": "flagged SUSPECT, left untouched", "swap": "flagged for review",
        "drift_swap": "timing fixed and swaps flagged", "dropdup": "left untouched",
        "jitter": "no worse than injected", "clean": "left untouched, not flagged"}


def md_models():
    sm, mq, speed, ram = load()
    out = ["| Model | Errors handled, sampled | Errors handled, full | Healthy files left alone, sampled / full | Speed (x real time) | RAM | Word F1 |",
           "|---|---|---|---|---|---|---|"]
    for m in ORDER:
        if m not in sm["models"]:
            continue
        ps, ns = share(sm, m, "sampled")
        pf, nf = share(sm, m, "full")
        cs, cf = cell(sm, m, "sampled", "clean"), cell(sm, m, "full", "clean")
        sp = f"{speed[m]:.1f}" if m in speed else "network"
        rm = f"{ram[m] / 1000:.1f} GB" if m in ram else "-"
        name = f"**{NAME[m]}**" if m == DEFAULT else NAME[m]
        out.append(f"| {name} | {100 * ps / ns:.1f} % ({ps}/{ns}) | {100 * pf / nf:.1f} % ({pf}/{nf}) | "
                   f"{cs[0]}/{cs[1]} and {cf[0]}/{cf[1]} | {sp} | {rm} | {mq[m]['f1']:.2f} |")
    return "\n".join(out)


def md_errors():
    sm, *_ = load()
    out = ["| Error type | Passes when | Sampled | Full transcript | Range over all 15 models (sampled) |", "|---|---|---|---|---|"]
    for g in HEADLINE + ["clean"]:
        ps, pf = cell(sm, DEFAULT, "sampled", g), cell(sm, DEFAULT, "full", g)
        lo = min(cell(sm, m, "sampled", g)[0] for m in sm["models"])
        hi = max(cell(sm, m, "sampled", g)[0] for m in sm["models"])
        rng = f"{lo}-{hi} of {ps[1]}" if lo != hi else f"{hi} of {ps[1]}"
        out.append(f"| {GROUP_NAME[g]} | {RULE[g]} | {ps[0]}/{ps[1]} | {pf[0]}/{pf[1]} | {rng} |")
    return "\n".join(out)


def md_bins():
    sm, *_ = load()
    h = sm["hole_detection_by_removed_dialogue_lines"]
    c = sm["cut_end_detection_by_cut_length"]
    out = ["| Hole removed (dialogue lines) | Detected |", "|---|---|"]
    for k in ("0-9", "10-19", "20-39", "40+"):
        p, n = h[k]
        out.append(f"| {k} | {p}/{n} ({100 * p / n:.0f} %) |")
    out += ["", "| Start or end cut off | Detected |", "|---|---|"]
    for k in ("< 150 s", "150-250 s", ">= 250 s"):
        p, n = c[k]
        out.append(f"| {k} | {p}/{n} ({100 * p / n:.0f} %) |")
    return "\n".join(out)


def inject(path, name, text):
    import re
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    pat = re.compile(rf"(<!-- table:{name} -->).*?(<!-- /table:{name} -->)", re.S)
    if not pat.search(s):
        return False
    p.write_text(pat.sub(lambda m: m.group(1) + "\n" + text + "\n" + m.group(2), s), encoding="utf-8")
    return True



def models():
    sm, mq, speed, _ram = load()
    models_ = [m for m in ORDER if m in sm["models"]]
    W, H = 1130, 96 + 28 * len(models_) + 40
    x0, pw = [262, 470, 678, 886], 130
    b = ['<text class="t1" x="20" y="28" font-size="16" font-weight="600">Fifteen model configurations on the same ten verified episodes</text>',
         '<text class="t2" x="20" y="48" font-size="12.5">Each model gets the same injected errors. Detection is nearly the same for all; speed and word accuracy are not.</text>']
    heads = ["Injected errors handled", "Healthy files left alone", "Speed (x realtime, CPU)", "Word accuracy (F1)"]
    for i, hd in enumerate(heads):
        b.append(f'<text class="t2" x="{x0[i]}" y="76" font-size="12" font-weight="600">{hd}</text>')
    for j, m in enumerate(models_):
        y = 88 + 28 * j
        hi = m == DEFAULT
        cls = "hi" if hi else "lo"
        w = ' font-weight="600"' if hi else ""
        b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5"{w}>{NAME[m]}</text>')
        p, n = share(sm, m, "sampled")
        cp, cn = cell(sm, m, "sampled", "clean")
        vals = [(p / n if n else 0, f"{100 * p / n:.1f} %" if n else "-"),
                (cp / cn if cn else 0, f"{cp}/{cn}"),
                (min(speed[m] / 30.0, 1.0) if m in speed else None, f"{speed[m]:.1f}x" if m in speed else "network"),
                (mq.get(m, {}).get("f1", 0), f"{mq[m]['f1']:.2f}" if m in mq else "-")]
        for i, (frac, label) in enumerate(vals):
            b.append(f'<rect class="track" x="{x0[i]}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
            if frac is None:
                b.append(f'<text class="t2" x="{x0[i] + 8}" y="{y + 16}" font-size="12">{label}</text>')
                continue
            b.append(f'<rect class="{cls}" x="{x0[i]}" y="{y + 3}" width="{pw * frac:.1f}" height="16" rx="3"/>')
            b.append(f'<text class="t1" x="{x0[i] + pw + 6}" y="{y + 16}" font-size="12">{label}</text>')
    b.append(f'<text class="t2" x="20" y="{H - 14}" font-size="11.5">Production setup (sampled mode), {sm["ok_rows"] // 2 // len(sm["models"])} runs per model per mode. '
             f'Speed: 4 CPU threads, five-minute excerpts. Bars start at 0.</text>')
    return svg(W, H, "".join(b), "Model comparison on the Known Good episodes",
               "Detection and repair are close to the same for every model; the default tiny.en is the fastest and has no false alarms on the healthy files.")


def errors():
    sm, *_ = load()
    groups = HEADLINE + ["clean"]
    W, x0, pw = 900, 290, 260
    H = 84 + 36 * len(groups) + 40
    b = [f'<text class="t1" x="20" y="28" font-size="16" font-weight="600">Injected errors, production model ({NAME[DEFAULT]})</text>',
         '<text class="t2" x="20" y="48" font-size="12.5">Ten verified episodes, 58 scenarios. Passed / run: sampled mode (blue) and full-transcript mode (grey).</text>']
    for j, g in enumerate(groups):
        y = 70 + 36 * j
        b.append(f'<text class="t1" x="20" y="{y + 18}" font-size="12.5">{GROUP_NAME[g]}</text>')
        for k, (mode, cls) in enumerate((("sampled", "hi"), ("full", "lo"))):
            p, n = cell(sm, DEFAULT, mode, g)
            yy = y + 4 + k * 14
            b.append(f'<rect class="track" x="{x0}" y="{yy}" width="{pw}" height="11" rx="3"/>')
            if n:
                b.append(f'<rect class="{cls}" x="{x0}" y="{yy}" width="{pw * p / n:.1f}" height="11" rx="3"/>')
            b.append(f'<text class="t1" x="{x0 + pw + 8}" y="{yy + 10}" font-size="11.5">{p}/{n}</text>')
    b.append(f'<text class="t2" x="20" y="{H - 12}" font-size="11.5">Pass rules: offsets and rate errors fixed to 0.25 s median; blocks, holes, swaps flagged; healthy and dropped/duplicated left untouched.</text>')
    return svg(W, H, "".join(b), "Pass rate per error type", "Pass rate per injected error type for the default model.")


def cost():
    sm, mq, speed, ram = load()
    rows = [(NAME[m], 58 / speed[m], m == DEFAULT, f"{ram[m] / 1000:.1f} GB RAM") for m in ORDER if m in speed]
    return bars_panel("What one 58-minute episode costs, per model", "Minutes of CPU transcription (4 threads, one episode at a time) and RAM. Shorter is better.",
                      rows, max(v for _, v, *_ in rows) * 1.05, lambda v: f"{v:.0f} min",
                      "Minutes = 58 / measured speed (tests/known_good/data/speed). Bars start at 0. Real runs vary with startup and load.", x0=230, pw=330)


def quality():
    sm, mq, *_ = load()
    rows = [(NAME[m], mq[m]["f1"], m == DEFAULT, f"{mq[m]['anchors_per_10min']} anchors / 10 min") for m in ORDER if m in mq]
    return bars_panel("Word accuracy against the verified subtitle", "F1 of the transcript against the subtitle text, and how many timing anchors it gives.",
                      rows, 1.0, lambda v: f"{v:.2f}", "Ten episodes. Bars start at 0. Source: tests/known_good/results/model_quality.json", x0=230, pw=330)

def songs():
    data = [("tiny.en", 1, 0, 3), ("base.en", 1, 5, 5), ("small.en", 4, 4, 5), ("medium.en", 3, 5, 5), ("large-v3-turbo", 0, 0, 0)]
    W, x0, pw = 860, 190, 150
    H = 112 + 30 * len(data) + 30
    b = ['<text class="t1" x="20" y="28" font-size="16" font-weight="600">Does the model mark music? 5 songs, 30 s of dialogue either side</text>',
         '<text class="t2" x="20" y="48" font-size="12.5">Songs with at least one music tag (♪, [MUSIC]) inside the song, out of 5. Prompt: “♪ [MUSIC] ♪ (upbeat music) [SINGING]”.</text>']
    for i, h in enumerate(["Default", "With --prompt", "--prompt + --carry-initial-prompt"]):
        b.append(f'<text class="t2" x="{x0 + i * 220}" y="78" font-size="12" font-weight="600">{h}</text>')
    for j, (n, *v) in enumerate(data):
        y = 90 + 30 * j
        b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5">{n}</text>')
        for i, k in enumerate(v):
            x = x0 + i * 220
            b.append(f'<rect class="track" x="{x}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
            if k:
                b.append(f'<rect class="hi" x="{x}" y="{y + 3}" width="{pw * k / 5:.1f}" height="16" rx="3"/>')
            b.append(f'<text class="t1" x="{x + pw + 8}" y="{y + 16}" font-size="12">{k}/5</text>')
    b.append(f'<text class="t2" x="20" y="{H - 8}" font-size="11.5">Bars start at 0. Prompting also adds false marks in the surrounding dialogue. Source: music test, docs/models_and_tests.md</text>')
    return svg(W, H, "".join(b), "Music marking per model and setting", "Prompt plus carry makes base, small and medium mark all 5 songs; large-v3-turbo marks none.")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, fn in (("models", models), ("errors", errors), ("cost", cost), ("quality", quality), ("songs", songs)):
        (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
    ROOT = Path(__file__).resolve().parent.parent
    for name, fn in (("models", md_models), ("errors", md_errors), ("bins", md_bins)):
        for f in (ROOT / "README.md", ROOT / "docs" / "models_and_tests.md"):
            if f.exists():
                inject(f, name, fn())
    print("ok")
