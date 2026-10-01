"""Writes docs/img/models.svg and docs/img/errors.svg for the README.
Data: docs/modelvalg_godkendte.md (numbers copied from it by hand; update both together).
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


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "models.svg").write_text(models(), encoding="utf-8")
    (OUT / "errors.svg").write_text(errors(), encoding="utf-8")
    print("ok")
