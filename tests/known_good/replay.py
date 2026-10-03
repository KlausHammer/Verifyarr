"""Dataset mode for tests/e2e_matrix.py: run the matrix from tests/known_good/data/ with no audio or video.

Activated by the environment variable VERIFYARR_KG (see README.md):

    VERIFYARR_KG=replay   everything comes from data/. alass answers are replayed from data/alass/.
    VERIFYARR_KG=auto     like replay, but a missing alass answer is computed with the real alass and the real
                          audio (needs the maintainer's staging folder) and recorded.

What is replaced, and why it is faithful:
  * the subtitle under test      data/subtitles/<slug>.srt (the user-verified original)
  * the Whisper evidence         data/transcripts/<model>/<slug>.json.gz (stored whisper.cpp output)
  * alass                        answers stored per (episode, exact input subtitle, flags); alass is deterministic
  * the media duration           data/episodes.json
Everything else in the pipeline is the real code. The VAD sidecars in data/vad/ are shipped as data; note that the
pipeline does not consult a VAD binary for video files at all (vad.run_vad_timeline only reads WAV), so nothing is lost.
"""
from __future__ import annotations

import atexit
import gzip
import hashlib
import lzma
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("VERIFYARR_KG_DATA", HERE / "data"))
STAGING = Path(os.environ.get("VERIFYARR_TEST_DATA", "/mnt/c/Users/knham/Desktop/undertekst auto")) / "whisper_gpu_staging"

# Canonical model order (matches the report tables).
MODEL_ORDER = ["tiny.en-greedy-cpu", "tiny.en-cpu", "tiny.en-q5_1-cpu", "base.en-greedy-cpu", "base.en-cpu",
               "base.en-q5_1-cpu", "small.en-greedy", "small.en", "small.en-q5_1", "medium.en-greedy", "medium.en",
               "medium.en-q5_0", "turbo-q8_0", "turbo-q5_0", "groq-turbo"]

_ALASS: dict[str, dict] = {}        # slug -> {key: entry}
_NEW: dict[str, list] = {}          # slug -> entries recorded by this process


def _load_alass(slug):
    if slug in _ALASS:
        return _ALASS[slug]
    table = {}
    for p in sorted((DATA / "alass").glob(f"{slug}.*jsonl.*")):
        for e in _read_entries(p):
            table[e["k"]] = e
    _ALASS[slug] = table
    return table


def _read_entries(p):
    opener = lzma.open if p.suffix == ".xz" else gzip.open
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _flush_new():
    d = DATA / "alass"
    for slug, entries in _NEW.items():
        if not entries:
            continue
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{slug}.part{os.getpid()}.jsonl.xz"
        with lzma.open(out, "wt", encoding="utf-8", preset=6) as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def merge_alass():
    """Folds data/alass/<slug>.part*.jsonl.* (and an older <slug>.jsonl.gz) into one data/alass/<slug>.jsonl.xz per
    episode. xz is used because every answer repeats the same subtitle text: its large dictionary shrinks the file
    about nine times against gzip."""
    d = DATA / "alass"
    slugs = sorted({p.name.split(".")[0] for p in d.glob("*.jsonl.*")})
    for slug in slugs:
        table = {}
        files = sorted(d.glob(f"{slug}.*jsonl.*"))
        for p in files:
            for e in _read_entries(p):
                table[e["k"]] = e
        tmp = d / f"{slug}.merged.tmp"
        with lzma.open(tmp, "wt", encoding="utf-8", preset=9) as f:
            for k in sorted(table):
                f.write(json.dumps(table[k], ensure_ascii=False, separators=(",", ":")) + "\n")
        for p in files:
            p.unlink()
        tmp.rename(d / f"{slug}.jsonl.xz")
        print(f"{slug}: {len(table)} alass answers")


def install(M):
    mode = os.environ.get("VERIFYARR_KG", "replay")
    episodes = json.loads((DATA / "episodes.json").read_text(encoding="utf-8"))
    from verifyarr import correctness, line_order, pipeline
    from verifyarr.subtitles import load_subs

    M.SLUGS = list(episodes)
    M.DRIFT_CASE_SLUGS = set()
    M.REAL_CASE_SLUGS = set()
    present = [m for m in MODEL_ORDER if (DATA / "transcripts" / m).is_dir()]
    M.SWEEP_MODELS = present
    M.ALL_MODELS = present
    M.OPT_IN_MODELS = []
    M.VIDEO_OPTIONAL = True

    def fixture(slug):
        e = episodes[slug]
        return {"slug": slug, "video_name": f"{slug}.mkv", "subtitle_name": f"{slug}.srt",
                "duration_seconds": e["duration_seconds"], "language": e["language"]}

    M.fixture = fixture
    M.subs_for = lambda slug, fx: load_subs(DATA / "subtitles" / f"{slug}.srt")
    M.media_dir = lambda slug: DATA / "media"          # never exists; nothing reads it in dataset mode

    def audio_evidence(model, slug, fx):
        p = DATA / "transcripts" / model / f"{slug}.json.gz"
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        return doc["language"], doc["segments"]

    M.audio_evidence = audio_evidence

    def pseudo_wav(slug):
        return Path(os.environ.get("VERIFYARR_KG_PSEUDO_AUDIO", "/nonexistent-audio")) / f"{slug}.wav"

    def audio_cache_for(slug, video):
        if mode == "auto":
            real = STAGING / "wav" / f"{slug}.wav"
            if real.exists():
                return {video: real}
        return {video: pseudo_wav(slug)}

    M.audio_cache_for = audio_cache_for

    # Durations come from the dataset (the files are not there to probe).
    real_dur = correctness.get_duration_seconds

    def duration(path):
        stem = Path(str(path)).stem
        if stem in episodes:
            return float(episodes[stem]["duration_seconds"])
        return real_dur(path)

    for mod in (correctness, line_order, pipeline):
        if hasattr(mod, "get_duration_seconds"):
            mod.get_duration_seconds = duration
    M._get_duration = duration
    # The audio track's language tag is not available either; every episode here is English by construction.
    for mod in (correctness, line_order, pipeline):
        if hasattr(mod, "detect_audio_language_ffprobe"):
            mod.detect_audio_language_ffprobe = lambda path: "en"

    # alass: replay by (episode, input subtitle bytes, flags).
    real_run = pipeline.run_alass
    real_bin = pipeline.resolve_alass_bin

    def run_alass(alass_bin, reference_path, subtitle_path, out_path, split_penalty, timeout=900, no_splits=False):
        slug = Path(str(reference_path)).stem
        key = hashlib.sha1(Path(subtitle_path).read_bytes()).hexdigest() + ("|ns" if no_splits else f"|sp{split_penalty}")
        table = _load_alass(slug)
        e = table.get(key)
        if e is None:
            if mode != "auto":
                raise RuntimeError(f"alass replay miss for {slug} ({key[:12]}...); rerun the missing rows with VERIFYARR_KG=auto")
            ok, msg, tail = real_run(alass_bin, reference_path, subtitle_path, out_path, split_penalty, timeout, no_splits)
            srt = Path(out_path).read_text(encoding="utf-8", errors="replace") if ok and Path(out_path).exists() else None
            e = {"k": key, "ok": ok, "msg": msg, "tail": tail, "srt": srt}
            table[key] = e
            _NEW.setdefault(slug, []).append(e)
            return ok, msg, tail
        if e["ok"] and e["srt"] is not None:
            Path(out_path).write_text(e["srt"], encoding="utf-8")
        return e["ok"], e["msg"], e["tail"]

    pipeline.run_alass = run_alass
    pipeline.resolve_alass_bin = lambda: real_bin() or "alass"
    M.LOCAL_VAD_BINARY = Path(sys.executable)
    M.LOCAL_VAD_MODEL = Path(sys.executable)
    atexit.register(_flush_new)


if __name__ == "__main__" and sys.argv[1:] == ["merge"]:
    merge_alass()
