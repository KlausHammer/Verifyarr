"""Writes docs/img/*.svg for the README and docs/modeller_og_sweeps.md.
Data: docs/modelvalg_godkendte.md, docs/modelrapport.md and the music test (numbers copied by hand; update both together).
Run: python3 docs/make_charts.py"""
from pathlib import Path

OUT = Path(__file__).parent / "img"
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


def models():
    rows = [  # name, pass of 242 (sampled = production), speed x realtime, word F1 (mean of SH, KG), chosen, cloud
        ("tiny.en (greedy) — chosen", 239, 25.0, 0.78, True, False),
        ("tiny.en", 236, 17.0, 0.79, False, False),
        ("base.en (greedy)", 239, 16.0, 0.83, False, False),
        ("small.en (greedy)", 240, 5.8, 0.88, False, False),
        ("small.en", 237, 4.5, 0.88, False, False),
        ("medium.en (greedy)", 237, 2.1, 0.90, False, False),
        ("medium.en", 237, 1.8, 0.89, False, False),
        ("large-v3-turbo q8_0", 238, 1.7, 0.89, False, False),
        ("large-v3-turbo q5_0", 238, 1.3, 0.89, False, False),
        ("cloud: Groq large-v3-turbo", 232, None, 0.89, False, True),
    ]
    W, H = 900, 78 + 30 * len(rows) + 36
    x0 = [250, 470, 690]            # panel left edges
    pw = 170                       # panel width
    b = [f'<text class="t1" x="20" y="28" font-size="16" font-weight="600">Ten Whisper models, same 242 injected-error tests each</text>',
         f'<text class="t2" x="20" y="48" font-size="12.5">Detection and repair are the same for every model; the cost is not.</text>']
    heads = ["Tests passed (of 242)", "Speed (× realtime, CPU)", "Word accuracy (F1)"]
    for i, hd in enumerate(heads):
        b.append(f'<text class="t2" x="{x0[i]}" y="76" font-size="12" font-weight="600">{hd}</text>')
    top = 88
    for j, (name, ok, sp, f1, hi, cloud) in enumerate(rows):
        y = top + 30 * j
        cls = "hi" if hi else "lo"
        weight = ' font-weight="600"' if hi else ""
        b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5"{weight}>{name}</text>')
        # panel 1
        b.append(f'<rect class="track" x="{x0[0]}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
        b.append(f'<rect class="{cls}" x="{x0[0]}" y="{y + 3}" width="{pw * ok / 242:.1f}" height="16" rx="3"/>')
        b.append(f'<text class="t1" x="{x0[0] + pw + 6}" y="{y + 16}" font-size="12">{ok}</text>')
        # panel 2
        b.append(f'<rect class="track" x="{x0[1]}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
        if sp is None:
            b.append(f'<text class="t2" x="{x0[1] + 8}" y="{y + 16}" font-size="12">network-bound</text>')
        else:
            b.append(f'<rect class="{cls}" x="{x0[1]}" y="{y + 3}" width="{pw * sp / 30:.1f}" height="16" rx="3"/>')
            b.append(f'<text class="t1" x="{x0[1] + pw + 6}" y="{y + 16}" font-size="12">{sp:g}×</text>')
        # panel 3
        b.append(f'<rect class="track" x="{x0[2]}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
        b.append(f'<rect class="{cls}" x="{x0[2]}" y="{y + 3}" width="{pw * f1:.1f}" height="16" rx="3"/>')
        b.append(f'<text class="t1" x="{x0[2] + pw + 6}" y="{y + 16}" font-size="12">{f1:.2f}</text>')
    b.append(f'<text class="t2" x="20" y="{H - 14}" font-size="11.5">Speed: 4 CPU threads, one episode at a time. Bars start at 0. '
             f'Source: docs/modelvalg_godkendte.md</text>')
    return svg(W, H, "".join(b), "Whisper model comparison",
               "tiny.en greedy passes 239 of 242 tests like the larger models, runs 25 times realtime and has the lowest word accuracy, 0.78.")


def errors():
    groups = [
        ("Fixed automatically", [("Constant offset", 63, 66, "3 misses: +0.3 s, just over the 0.25 s bar"),
                                 ("Framerate, PAL, drift", 66, 66, "")]),
        ("Detected and flagged (never rewritten wrong)", [("Mistimed blocks", 44, 44, ""),
                                                          ("Missing middle", 11, 11, ""),
                                                          ("Wrong episode", 11, 11, ""),
                                                          ("Swapped lines", 11, 11, ""),
                                                          ("Drift + swapped lines", 11, 11, "")]),
        ("Must not make things worse", [("Healthy file (no false alarm)", 11, 11, ""),
                                        ("Dropped / duplicated cues", 11, 11, ""),
                                        ("Per-line jitter (nothing to fix)", 8, 11, "alass chases the noise (all models)")]),
    ]
    W, x0, pw = 860, 270, 280
    y = 70
    b = ['<text class="t1" x="20" y="28" font-size="16" font-weight="600">Injected errors, production setup (tiny.en + VAD, sampled)</text>',
         '<text class="t2" x="20" y="48" font-size="12.5">11 approved episodes × 23 scenarios. Passed / run, per error type.</text>']
    for g, items in groups:
        b.append(f'<text class="t2" x="20" y="{y + 12}" font-size="12" font-weight="600">{g}</text>')
        y += 22
        for name, ok, n, note in items:
            b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5">{name}</text>')
            b.append(f'<rect class="track" x="{x0}" y="{y + 3}" width="{pw}" height="16" rx="3"/>')
            b.append(f'<rect class="hi" x="{x0}" y="{y + 3}" width="{pw * ok / n:.1f}" height="16" rx="3"/>')
            b.append(f'<text class="t1" x="{x0 + pw + 8}" y="{y + 16}" font-size="12">{ok}/{n}</text>')
            if note:
                b.append(f'<text class="t2" x="{x0 + pw + 52}" y="{y + 16}" font-size="11.5">{note}</text>')
            y += 26
        y += 8
    H = y + 22
    b.append(f'<text class="t2" x="20" y="{H - 8}" font-size="11.5">Pass = fixed to within 0.25 s median and 98 % of lines within 1 s, or (for blocks / holes) flagged. Bars start at 0.</text>')
    return svg(W, H + 6, "".join(b), "Pass rate per error type",
               "All error types pass 100 percent except constant offset 63 of 66 (a 0.3 second boundary case) and per-line jitter 8 of 11.")


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


def cost():
    rows = [("tiny.en (greedy) — chosen", 25.0, 0.6, True), ("tiny.en", 17.0, 0.6, False), ("base.en (greedy)", 16.0, 0.7, False),
            ("small.en (greedy)", 5.8, 1.2, False), ("small.en", 4.5, 1.3, False), ("medium.en (greedy)", 2.1, 2.4, False),
            ("medium.en", 1.8, 2.7, False), ("large-v3-turbo q8_0", 1.7, 1.8, False), ("large-v3-turbo q5_0", 1.3, 1.5, False)]
    out = [(n, 58 / sp, hi, f"{ram} GB RAM") for n, sp, ram, hi in rows]
    return bars_panel("What one 58-minute episode costs, per model", "Minutes of CPU transcription (4 threads, one episode at a time) and RAM. Shorter is better.",
                      out, 46, lambda v: f"{v:.0f} min", "Minutes = 58 / speed from docs/modelvalg_godkendte.md. Bars start at 0. Real runs vary with startup and load.", x0=230, pw=330)


def agreement():
    rows = [("large-v3-turbo (reference)", .91, .94, False), ("turbo q8_0", .88, .94, False), ("medium.en (greedy)", .88, .94, False),
            ("small.en (greedy)", .87, .93, False), ("small.en / q5_1", .86, .93, False), ("base.en", .84, .90, False), ("tiny.en", .80, .87, True)]
    W, x0, pw = 860, 250, 440
    H = 78 + 28 * len(rows) + 40
    sx = lambda v: x0 + pw * (v - .70) / (.96 - .70)
    b = ['<text class="t1" x="20" y="28" font-size="16" font-weight="600">How well each model\'s words line up with the subtitle</text>',
         '<text class="t2" x="20" y="48" font-size="12.5">Agreement = share of subtitle words found within ±2 s in the transcript; range over episodes E02/E03/E04.</text>']
    for t in (.70, .75, .80, .85, .90, .95):
        b.append(f'<line class="grid" x1="{sx(t):.1f}" y1="64" x2="{sx(t):.1f}" y2="{H - 34}"/>')
        b.append(f'<text class="t2" x="{sx(t):.1f}" y="{H - 20}" font-size="11" text-anchor="middle">{t:.2f}</text>')
    for j, (n, lo, hi_, ch) in enumerate(rows):
        y = 70 + 28 * j
        w = ' font-weight="600"' if ch else ""
        b.append(f'<text class="t1" x="20" y="{y + 15}" font-size="12.5"{w}>{n}</text>')
        b.append(f'<rect class="{"hi" if ch else "lo"}" x="{sx(lo):.1f}" y="{y + 4}" width="{sx(hi_) - sx(lo):.1f}" height="14" rx="3"/>')
        b.append(f'<text class="t1" x="{sx(hi_) + 8:.1f}" y="{y + 16}" font-size="12">{lo:.2f}–{hi_:.2f}</text>')
    b.append(f'<text class="t2" x="20" y="{H - 4}" font-size="11.5">All 14 configurations are viable (≥ 0.80). Axis starts at 0.70. Source: docs/modelrapport.md</text>')
    return svg(W, H + 4, "".join(b), "Model agreement", "Agreement runs from 0.80-0.87 for tiny.en to 0.91-0.94 for large-v3-turbo.")


def clips():
    return bars_panel("How many clips are needed to find the offset?", "Share of probes within ±2 s of the true block offset, by clips per 10 minutes of episode.",
                      [("1 clip per 10 min", .953, False, ""), ("2 clips per 10 min", .994, True, "sweet spot, about 10 % of the audio"), ("3 clips per 10 min", .981, False, "misses sit at jumps")],
                      1.0, lambda v: f"{v * 100:.1f} %", "Probe = 30 s transcript. Source: docs/modelrapport.md", x0=190, pw=300)


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
    b.append(f'<text class="t2" x="20" y="{H - 8}" font-size="11.5">Bars start at 0. Prompting also adds false marks in the surrounding dialogue. Source: music test, docs/modeller_og_sweeps.md</text>')
    return svg(W, H, "".join(b), "Music marking per model and setting", "Prompt plus carry makes base, small and medium mark all 5 songs; large-v3-turbo marks none.")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "models.svg").write_text(models(), encoding="utf-8")
    (OUT / "errors.svg").write_text(errors(), encoding="utf-8")
    for name, fn in (("cost", cost), ("agreement", agreement), ("clips", clips), ("songs", songs)):
        (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
    print("ok")
