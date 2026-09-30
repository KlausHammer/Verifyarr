"""Random-offset sync test: non-uniform subtitle corruption.

The earlier e2e (e2e_after.py) only tested a UNIFORM +45s global shift. Real-world
desync is rarely uniform: framerate conversions cause progressive drift, bad rips
shift different parts differently, and cut versions delete whole chunks. This
script applies three non-uniform corruptions per episode and measures how well
the REAL pipeline (sync_pair + correctness_and_finish, real alass, fixture
transcript shim -- same harness shape as e2e_after.py) recovers the ORIGINAL
timings:

  piecewise  -- 6 contiguous blocks, each shifted by a seeded random +/-5..15s
  drift      -- progressive 2% stretch (t -> t*1.02), like a 25<->23.976fps error
  gap        -- a 5-minute middle chunk deleted (cut version); surviving lines
                must keep their timing

Scoring is against the episode's own on-disk subtitle timings (only episodes
whose on-disk subs are independently known to be correct+in-sync are used --
see min_coverage.GOOD). No new transcription is needed: corruption is applied
to the SRT text, and the fixture transcript shim supplies the audio evidence.

Output (next to this file, reusable without re-running):
  e2e_offset{SUFFIX}.jsonl          one row per slug x scenario with recovery metrics
  e2e_offset_summary{SUFFIX}.json   per-scenario aggregates
Run: .venv/bin/python verifyarr_handoff/e2e_offset.py [--only C_S02E01,...]
"""
from __future__ import annotations
import contextlib
import copy
import json
import random
import statistics
import sys
import time
from pathlib import Path

import os as _os
# code under test: repo checkout including patches 0001-0004. Override with
# VERIFYARR_UNDER_TEST=/home/hammer/Auto\ sync\ sub/verifyarr when running from the repo itself.
VWORK = _os.environ.get("VERIFYARR_UNDER_TEST", "/tmp/vwork")
sys.path.insert(0, VWORK)

from verifyarr import db, generate, pipeline
from verifyarr.correctness import full_transcript_cache_key
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs

# --- fixture access (same sources as oracle.py, inlined to avoid its hardcoded
# --- sys.path inserts pointing at the pre-patch checkout)
FIX = Path("/home/hammer/Auto sync sub/verifyarr/tests/fixtures/whisper_full")
DIRS = {
    "C_S02": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 2"),
    "C_S03": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 3"),
    "SH_S01": Path("/mnt/c/Users/knham/Desktop/undertekst auto/Slow Horse/Season 1"),
}
# Episodes whose on-disk subtitle is genuinely this episode's text AND in sync
# (independently established; complement of min_coverage.BAD), spread over series.
SLUGS = ["C_S02E01", "C_S02E05", "C_S02E09", "C_S02E12",
         "C_S03E04", "C_S03E09", "C_S03E16", "SH_S01E02"]

OUT_DIR = Path(__file__).parent
SHARD = sys.argv[sys.argv.index("--shard") + 1] if "--shard" in sys.argv else ""
SUFFIX = f"_{SHARD}" if SHARD else ""
WORK = OUT_DIR / f"e2e_work_offset{SUFFIX}"
WORK.mkdir(exist_ok=True)


def media_dir(slug):
    for p, d in DIRS.items():
        if slug.startswith(p):
            return d
    raise KeyError(slug)


def fixture(slug):
    return json.loads((FIX / f"{slug}.json").read_text(encoding="utf-8"))


def subs_for(slug):
    return load_subs(media_dir(slug) / fixture(slug)["subtitle_name"])


# --- transcript shim (same as run_s3_slowhorses_validation.patch_whisper_full:
# --- no STT calls, fixture segments are the audio evidence)
@contextlib.contextmanager
def patch_whisper_full(fx: dict):
    real = generate.full_transcript_for_check

    def fake(cfg, video_path, tmp_dir, conn, cancel_event=None):
        return fx["language"], fx["segments"]

    generate.full_transcript_for_check = fake
    try:
        yield
    finally:
        generate.full_transcript_for_check = real


def cfg_for(conn, **over) -> Config:
    cfg = Config.from_db(conn)
    vals = dict(
        backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, line_order_enabled=True,
        line_order_audio_confirm=True, whisper_mode="full",
        # Local Whisper, like production -- the cloud providers only generate missing subtitles.
        local_whisper_binary=sys.executable,
    )
    vals.update(over)
    for k, v in vals.items():
        object.__setattr__(cfg, k, v)
    return cfg


def run_one(video, subs, fx, cfg, conn, tag):
    from verifyarr import db as _db
    _provider, _model = full_transcript_cache_key(cfg)
    _db.save_full_transcript_cache(conn, video, fx["language"], fx["segments"],
                                   stt_provider=_provider, stt_model=_model)
    tmp = WORK / f"{tag}.srt"
    subs.save(str(tmp))
    with patch_whisper_full(fx):
        row, cur = pipeline.sync_pair(video, tmp, "en", cfg, {}, WORK)
        row = pipeline.correctness_and_finish(video, tmp, "en", cfg, conn, row, cur)
    from verifyarr.subtitles import load_subs as _load
    return row, _load(tmp)


# --- corruptions (all deterministic; RNG seeded in main) ---
def corrupt_piecewise(subs, rng, n_blocks=6, lo=5.0, hi=15.0):
    """Each contiguous time block shifted by its own random +/-lo..hi seconds."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    shifts = []
    for b in range(n_blocks):
        mag = rng.uniform(lo, hi) * rng.choice((-1, 1))
        shifts.append(mag)
        b0, b1 = dur * b / n_blocks, dur * (b + 1) / n_blocks
        for e in out.events:
            if b0 <= e.start / 1000.0 < b1:
                e.start = max(0, int(e.start + mag * 1000))
                e.end = max(e.start + 200, int(e.end + mag * 1000))
    return out, None, {"block_shifts_s": [round(s, 2) for s in shifts]}


def corrupt_drift(subs, rng, rate=0.02):
    """Progressive stretch t -> t*(1+rate): framerate-conversion style drift."""
    out = copy.deepcopy(subs)
    for e in out.events:
        e.start = int(e.start * (1 + rate))
        e.end = int(e.end * (1 + rate))
    return out, None, {"rate": rate}


def corrupt_gap(subs, rng, gap_seconds=300.0):
    """Delete a contiguous middle chunk (cut version). Returns kept indices."""
    out = copy.deepcopy(subs)
    dur = max(e.end for e in out.events) / 1000.0
    g0, g1 = dur * 0.4, dur * 0.4 + gap_seconds
    kept = [i for i, e in enumerate(out.events)
            if not (g0 * 1000 <= e.start and e.end <= g1 * 1000)]
    removed = [i for i in range(len(out.events)) if i not in set(kept)]
    out.events = [out.events[i] for i in kept]
    return out, kept, {"gap_start_s": round(g0, 1), "gap_end_s": round(g1, 1),
                       "removed_lines": len(removed)}


def timing_errors(ref_events, after_events, kept=None):
    """Per-line |start_after - start_ref| in seconds over matched indices."""
    idx = kept if kept is not None else range(len(ref_events))
    errs = []
    for j, i in enumerate(idx):
        if j >= len(after_events):
            break
        errs.append(abs(after_events[j].start - ref_events[i].start) / 1000.0)
    return errs


def summarize(errs):
    if not errs:
        return {"n": 0}
    s = sorted(errs)
    return {
        "n": len(errs),
        "p50": round(statistics.median(s), 3),
        "p90": round(s[min(len(s) - 1, int(0.9 * len(s)))], 3),
        "frac_le_0_5s": round(sum(1 for e in s if e <= 0.5) / len(s), 3),
        "frac_le_1_0s": round(sum(1 for e in s if e <= 1.0) / len(s), 3),
        "frac_le_2_0s": round(sum(1 for e in s if e <= 2.0) / len(s), 3),
    }


SCENARIOS = {"piecewise": corrupt_piecewise, "drift": corrupt_drift, "gap": corrupt_gap}


def main():
    only = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
    scen = sys.argv[sys.argv.index("--scenarios") + 1].split(",") \
        if "--scenarios" in sys.argv else list(SCENARIOS)
    conn = db.connect(WORK / "e2e_offset.db")
    cfg = cfg_for(conn)
    results = []
    for slug in (only or SLUGS):
        try:
            fx, subs = fixture(slug), subs_for(slug)
        except Exception as e:
            print(f"{slug}: skipped ({e})", flush=True)
            continue
        video = media_dir(slug) / fx["video_name"]
        if not video.exists():
            print(f"{slug}: skipped (no video {video.name})", flush=True)
            continue
        ref = list(subs.events)
        t0 = time.time()
        for name in scen:
            # Seeded per slug+scenario (not one running sequence) so sharded
            # parallel runs produce byte-identical corruptions to a serial run.
            s_rng = random.Random(f"offset-v1:{slug}:{name}")
            corrupted, kept, detail = SCENARIOS[name](subs, s_rng)
            injected = summarize(timing_errors(ref, list(corrupted.events), kept))
            try:
                row, after = run_one(video, corrupted, fx, cfg, conn, f"{slug}.{name}")
                recovered = summarize(timing_errors(ref, list(after.events), kept))
                rec = dict(slug=slug, scenario=name, detail=detail,
                           injected_p50=injected.get("p50"),
                           flag=row.get("correctness_flag"), sync=row.get("sync_status"),
                           lo_fixed=row.get("line_order_fixed"),
                           recovered=recovered, note=(row.get("note") or "")[:200])
            except Exception as e:
                rec = dict(slug=slug, scenario=name, detail=detail,
                           injected_p50=injected.get("p50"), error=f"{type(e).__name__}: {e}"[:200])
            results.append(rec)
            r = rec.get("recovered", {})
            print(f"{slug}.{name}: injected_p50={rec.get('injected_p50')} "
                  f"-> rec_p50={r.get('p50')} rec<=1s={r.get('frac_le_1_0s')} "
                  f"flag={rec.get('flag')} sync={rec.get('sync')}", flush=True)
        print(f"{slug}: done ({time.time()-t0:.0f}s)", flush=True)
        (OUT_DIR / f"e2e_offset{SUFFIX}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in results), encoding="utf-8")
    summary = {}
    for name in scen:
        rows = [r for r in results if r["scenario"] == name and "recovered" in r]
        summary[name] = {
            "runs": len(rows),
            "errors": sum(1 for r in results if r["scenario"] == name and "error" in r),
            "mean_p50": round(statistics.mean([r["recovered"]["p50"] for r in rows]), 3) if rows else None,
            "mean_frac_le_1s": round(statistics.mean(
                [r["recovered"]["frac_le_1_0s"] for r in rows]), 3) if rows else None,
        }
    (OUT_DIR / f"e2e_offset_summary{SUFFIX}.json").write_text(
        json.dumps({"slugs": only or SLUGS, "summary": summary}, indent=1), encoding="utf-8")
    print("wrote", OUT_DIR / f"e2e_offset{SUFFIX}.jsonl", len(results), "rows;", summary)


if __name__ == "__main__":
    main()
