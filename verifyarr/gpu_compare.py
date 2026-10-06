"""CPU vs GPU Whisper on the same audio: is the timing the same, and how fast is each?

Run in the published image, no app needed:
  docker run --rm --device /dev/dri -v /media:/media:ro -v ./out:/out \\
    ghcr.io/klaushammer/verifyarr:latest python3 -m verifyarr.gpu_compare /media/series --random 4 --out /out
Per clip it transcribes with the app's own whisper flags on GPU, on CPU (-ng) and on CPU again
(the noise floor), then writes the transcripts plus report.md / report.json to --out.
With --pipeline it also runs the whole app pipeline on each episode twice (GPU on / off, own
database each) and saves both resulting subtitles next to the original for comparison."""
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

import shutil

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


def _find_subtitle(video: Path) -> Path | None:
    for ext in (".en.srt", ".en.hi.srt", ".en.forced.srt", ".srt"):
        cand = video.with_suffix(ext)
        if cand.is_file():
            return cand
    return None


def _cues(path: Path) -> list[tuple[int, int, str]]:
    from verifyarr.subtitles import load_subs
    return [(e.start, e.end, e.plaintext) for e in load_subs(path).events]


def cue_diff(a: Path, b: Path) -> dict:
    """Cue-by-cue start drift between two subtitle files (cue order is kept by every fix)."""
    ca, cb = _cues(a), _cues(b)
    n = min(len(ca), len(cb))
    d = [(cb[i][0] - ca[i][0]) / 1000 for i in range(n)]
    ad = sorted(abs(x) for x in d)
    return {"cues_a": len(ca), "cues_b": len(cb), "identical": ca == cb,
            "changed_cues": sum(1 for x in d if abs(x) >= 0.001),
            "median_shift_s": round(statistics.median(d), 3) if d else None,
            "max_abs_shift_s": round(ad[-1], 3) if ad else None,
            "p95_abs_shift_s": round(ad[int(len(ad) * 0.95)], 3) if ad else None}


def run_pipeline(video: Path, sub: Path, work: Path, gpu: bool) -> dict:
    """The app's own single-file run on a private copy, with its own database (no shared cache)."""
    from verifyarr import db, jobs
    from verifyarr.settings import Config, set_settings_group
    import threading

    name = "gpu" if gpu else "cpu"
    root = work / name
    shutil.rmtree(root, ignore_errors=True)
    folder = root / "library" / video.parent.name
    folder.mkdir(parents=True)
    vlink = folder / video.name
    vlink.symlink_to(video)
    sub_copy = folder / sub.name
    shutil.copy2(sub, sub_copy)
    conn = db.connect(root / "app.db")
    set_settings_group(conn, "general", {"series_folder": str(root / "library")})
    set_settings_group(conn, "correctness", {"local_whisper_use_gpu": gpu})
    cfg = Config.from_db(conn)
    t0 = time.monotonic()
    run_id = jobs.create_run(conn, "gpu_compare", "single", False, False)
    jobs.execute_run(run_id, cfg, conn, threading.Event(), "single", trigger="gpu_compare",
                     video=vlink, subtitle=sub_copy, lang="en")
    secs = round(time.monotonic() - t0, 1)
    row = conn.execute("SELECT * FROM files WHERE subtitle_path = ?", (str(sub_copy),)).fetchone()
    log_lines = [f"{r['level']} {r['message']}" for r in
                 conn.execute("SELECT level, message FROM run_log_lines WHERE run_id = ? ORDER BY id", (run_id,))]
    keep = ("sync_status", "sync_max_shift_s", "correctness_flag", "reason", "correctness_avg_score",
            "note", "auto_action", "structural_change")
    out_sub = work / f"{name}.{sub.name}"
    shutil.copy2(sub_copy, out_sub)
    (work / f"{name}.log.txt").write_text("\n".join(log_lines))
    info = {k: (row[k] if row else None) for k in keep}
    conn.close()
    return {"seconds": secs, "subtitle": str(out_sub), "file": info}


def pipeline_compare(vid: Path, out: Path) -> dict | None:
    sub = _find_subtitle(vid)
    if sub is None:
        print(f"== {vid.name}: no subtitle next to the video, skipped", flush=True)
        return None
    work = out / "pipeline" / re.sub(r"[^A-Za-z0-9._-]+", "_", vid.stem)[:90]
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sub, work / f"original.{sub.name}")
    print(f"== pipeline {vid.name}", flush=True)
    res = {name: run_pipeline(vid, sub, work, gpu) for name, gpu in (("gpu", True), ("cpu", False))}
    orig = work / f"original.{sub.name}"
    row = {"video": str(vid), "subtitle": sub.name,
           "gpu": res["gpu"], "cpu": res["cpu"],
           "gpu_vs_original": cue_diff(orig, Path(res["gpu"]["subtitle"])),
           "cpu_vs_original": cue_diff(orig, Path(res["cpu"]["subtitle"])),
           "gpu_vs_cpu": cue_diff(Path(res["cpu"]["subtitle"]), Path(res["gpu"]["subtitle"]))}
    (work / "result.json").write_text(json.dumps(row, indent=1, default=str))
    print(f"   gpu {res['gpu']['seconds']}s {res['gpu']['file']['sync_status']}/{res['gpu']['file']['correctness_flag']}"
          f" | cpu {res['cpu']['seconds']}s {res['cpu']['file']['sync_status']}/{res['cpu']['file']['correctness_flag']}"
          f" | gpu-vs-cpu max {row['gpu_vs_cpu']['max_abs_shift_s']}s", flush=True)
    return row


def _pipeline_markdown(rows: list[dict]) -> str:
    lines = ["", "## Whole pipeline, GPU on vs off", "",
             "| episode | GPU s | CPU s | GPU result | CPU result | GPU change vs original (median / max s) | CPU change (median / max s) | GPU vs CPU max s | identical |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        g, c = r["gpu"]["file"], r["cpu"]["file"]
        go, co, gc = r["gpu_vs_original"], r["cpu_vs_original"], r["gpu_vs_cpu"]
        lines.append(f"| {Path(r['video']).stem[:55]} | {r['gpu']['seconds']} | {r['cpu']['seconds']} | "
                     f"{g['sync_status']}/{g['correctness_flag']} {g['reason'] or ''} | {c['sync_status']}/{c['correctness_flag']} {c['reason'] or ''} | "
                     f"{go['median_shift_s']} / {go['max_abs_shift_s']} | {co['median_shift_s']} / {co['max_abs_shift_s']} | "
                     f"{gc['max_abs_shift_s']} | {'yes' if gc['identical'] else 'no'} |")
    return "\n".join(lines) + "\n"


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
    ap.add_argument("--pipeline", action="store_true", help="also run the whole app pipeline with GPU on and off")
    ap.add_argument("--skip-clips", action="store_true", help="only the pipeline part")
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
    for vid in ([] if args.skip_clips else vids):
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
    md = _markdown(rows) if rows else "# CPU vs GPU Whisper\n"
    if args.pipeline:
        prow = [r for r in (pipeline_compare(v, out) for v in vids) if r]
        (out / "pipeline_report.json").write_text(json.dumps(prow, indent=1, default=str))
        md += _pipeline_markdown(prow)
    (out / "report.md").write_text(md)
    print(md)
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
