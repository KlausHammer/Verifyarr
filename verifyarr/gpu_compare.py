"""CPU vs GPU Whisper on the same audio: is the timing the same, and how fast is each?

Run in the published image, no app needed:
  docker run --rm --device /dev/dri -v /media:/media:ro -v ./out:/out \\
    ghcr.io/klaushammer/verifyarr:latest python3 -m verifyarr.gpu_compare /media/series --random 4 --out /out
Per clip it transcribes with the app's own whisper flags on GPU, on CPU (-ng) and on CPU again
(the noise floor), then writes the transcripts plus report.md / report.json to --out."""
from __future__ import annotations

import argparse
import difflib
import json
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from verifyarr.correctness import (_parse_local_whisper_json, extract_clip, get_duration_seconds,
                                   load_whisper_json)

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
BINARY, MODEL = "/usr/local/bin/whisper-cli", "/app/models/ggml-tiny.en.bin"


def _videos(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += [f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXT] if p.is_dir() else [p]
    return sorted(out)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def run_whisper(wav: Path, stem: Path, binary: str, model: str, gpu: bool, threads: int) -> dict:
    cmd = [binary, "-m", model, "-f", str(wav), "-oj", "-of", str(stem), "-t", str(threads),
           "-l", "en", "-mc", "0", "-bs", "1", "-bo", "1"]
    if not gpu:
        cmd.append("-ng")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    secs = time.monotonic() - t0
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr[-400:], "seconds": secs}
    devices = re.findall(r"ggml_vulkan: \d+ = ([^|\n]+)", proc.stderr)
    data = _parse_local_whisper_json(load_whisper_json(stem.with_suffix(".json")))
    return {"ok": True, "seconds": round(secs, 2), "segments": data["segments"],
            "device": devices[0].strip() if devices else None}


def compare(a: list[dict], b: list[dict]) -> dict:
    """Pairs segments by text (difflib over the normalised lines), then measures start/end drift."""
    ta, tb = [_norm(s["text"]) for s in a], [_norm(s["text"]) for s in b]
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    starts, ends = [], []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            sa, sb = a[blk.a + k], b[blk.b + k]
            starts.append(sb["start"] - sa["start"])
            ends.append(sb["end"] - sa["end"])
    n = max(len(a), len(b), 1)

    def q(vals, p):
        s = sorted(abs(v) for v in vals)
        return round(s[min(len(s) - 1, int(len(s) * p))], 3) if s else None
    return {"segments_a": len(a), "segments_b": len(b), "matched": len(starts),
            "matched_pct": round(100 * len(starts) / n, 1),
            "text_similarity": round(sm.ratio(), 3),
            "start_median_signed_s": round(statistics.median(starts), 3) if starts else None,
            "start_abs_p50_s": q(starts, 0.5), "start_abs_p95_s": q(starts, 0.95),
            "start_abs_max_s": q(starts, 1.0), "end_abs_p95_s": q(ends, 0.95)}


def clip_starts(duration: float, clip: int, count: int) -> list[float]:
    room = max(0.0, duration - clip)
    return [round(room * (i + 1) / (count + 1), 1) for i in range(count)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="video files or folders")
    ap.add_argument("--random", type=int, default=0, help="pick N random videos from the paths")
    ap.add_argument("--clips", type=int, default=2, help="clips per video")
    ap.add_argument("--clip-seconds", type=int, default=300)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--out", default="/out")
    ap.add_argument("--binary", default=BINARY)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    vids = _videos(args.paths)
    if args.random:
        vids = random.Random(args.seed).sample(vids, min(args.random, len(vids)))
    if not vids:
        print("no videos found", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for vid in vids:
        dur = get_duration_seconds(vid)
        if not dur:
            print(f"skip {vid.name}: no duration", file=sys.stderr)
            continue
        for start in clip_starts(dur, args.clip_seconds, args.clips):
            tag = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', vid.stem)[:80]}@{int(start)}s"
            d = out / tag
            d.mkdir(exist_ok=True)
            print(f"== {tag}", flush=True)
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / "clip.wav"
                if not extract_clip(vid, start, args.clip_seconds, wav):
                    print("   audio extract failed", flush=True)
                    continue
                runs = {name: run_whisper(wav, d / name, args.binary, args.model, gpu, args.threads)
                        for name, gpu in (("gpu", True), ("cpu", False), ("cpu2", False))}
            for name, r in runs.items():
                if r["ok"]:
                    (d / f"{name}.segments.json").write_text(json.dumps(r["segments"], indent=1))
                print(f"   {name}: {r['seconds']}s" + ("" if r["ok"] else f" FAILED {r['error']}"), flush=True)
            row = {"video": str(vid), "clip_start_s": start, "clip_seconds": args.clip_seconds,
                   "seconds": {k: r["seconds"] for k, r in runs.items()},
                   "gpu_device": runs["gpu"].get("device")}
            if all(r["ok"] for r in runs.values()):
                row["gpu_vs_cpu"] = compare(runs["cpu"]["segments"], runs["gpu"]["segments"])
                row["cpu_vs_cpu"] = compare(runs["cpu"]["segments"], runs["cpu2"]["segments"])
            rows.append(row)
            (out / "report.json").write_text(json.dumps(rows, indent=1))
    (out / "report.md").write_text(_markdown(rows))
    print(_markdown(rows))
    return 0


def _markdown(rows: list[dict]) -> str:
    lines = ["# CPU vs GPU Whisper", "",
             "| clip | GPU s | CPU s | device | GPU≈CPU matched | start p95 s | start max s | CPU≈CPU start p95 s |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        g, c = r.get("gpu_vs_cpu"), r.get("cpu_vs_cpu")
        name = f"{Path(r['video']).stem[:50]} @{int(r['clip_start_s'])}s"
        lines.append(f"| {name} | {r['seconds']['gpu']} | {r['seconds']['cpu']} | {r['gpu_device'] or 'NO GPU USED'} | "
                     f"{g['matched_pct'] if g else '-'}% | {g['start_abs_p95_s'] if g else '-'} | "
                     f"{g['start_abs_max_s'] if g else '-'} | {c['start_abs_p95_s'] if c else '-'} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
