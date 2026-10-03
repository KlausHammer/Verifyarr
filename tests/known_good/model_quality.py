"""Word accuracy and anchor quality per Whisper model on the Known Good episodes (no audio needed).

    python tests/known_good/model_quality.py [--out tests/known_good/results]

For every (model, episode): the stored transcript against the verified subtitle.
  F1        word recall/precision of the transcript against the subtitle text (difflib matching blocks)
  anchors   matched subtitle lines per 10 minutes of audio, and the share within 0.5 s of the episode median
  clips>=3  share of 30 s clips with at least ANCHOR_MIN_COUNT anchors (what the sampled mode needs)
Writes model_quality.json and model_quality.md.
"""
import difflib
import gzip
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from verifyarr.correctness import dense_anchor_points
from verifyarr.subtitles import ANCHOR_MIN_COUNT, load_subs, speech_text

DATA = HERE / "data"
tok = lambda s: re.findall(r"[a-z0-9']+", s.lower())


def main(argv):
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else HERE / "results"
    episodes = json.loads((DATA / "episodes.json").read_text(encoding="utf-8"))
    subs = {s: load_subs(DATA / "subtitles" / f"{s}.srt") for s in episodes}
    truth = {s: tok(" ".join(speech_text(e.text) or "" for e in subs[s])) for s in subs}
    result = {}
    for mdir in sorted((DATA / "transcripts").iterdir()):
        hit = sn = tn = a = a05 = c = cok = eps = 0
        mins = 0.0
        for slug in episodes:
            p = mdir / f"{slug}.json.gz"
            if not p.exists():
                continue
            with gzip.open(p, "rt", encoding="utf-8") as f:
                segs = json.load(f)["segments"]
            t = tok(" ".join(speech_text(s["text"]) or "" for s in segs))
            sm = difflib.SequenceMatcher(None, truth[slug], t, autojunk=False)
            hit += sum(b.size for b in sm.get_matching_blocks())
            sn += len(truth[slug])
            tn += len(t)
            samples = dense_anchor_points(subs[slug], segs)
            offs = [y - x for s in samples for x, y in s["anchor_points"]]
            if offs:
                med = statistics.median(offs)
                a += len(offs)
                a05 += sum(abs(o - med) <= 0.5 for o in offs)
                c += len(samples)
                cok += sum(len(s["anchor_points"]) >= ANCHOR_MIN_COUNT for s in samples)
            mins += max(s["end"] for s in segs) / 60.0
            eps += 1
        if not sn or not tn:
            continue
        rec, prec = hit / sn, hit / tn
        result[mdir.name] = {"episodes": eps, "recall": round(rec, 3), "precision": round(prec, 3),
                             "f1": round(2 * rec * prec / (rec + prec), 3),
                             "anchors_per_10min": round(a / mins * 10, 1) if mins else None,
                             "share_anchors_within_0.5s": round(a05 / a, 3) if a else None,
                             "clips_with_3plus_anchors": round(cok / c, 3) if c else None}
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_quality.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    lines = ["| model | episodes | recall | precision | F1 | anchors / 10 min | anchors within 0.5 s | clips with >= 3 anchors |", "|---|---|---|---|---|---|---|---|"]
    for m, v in result.items():
        lines.append(f"| {m} | {v['episodes']} | {v['recall']:.3f} | {v['precision']:.3f} | {v['f1']:.3f} | "
                     f"{v['anchors_per_10min']} | {v['share_anchors_within_0.5s']} | {v['clips_with_3plus_anchors']} |")
    (out / "model_quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv)
