"""Fixtures for the real flawed episodes (Z5_flaggede): small.en transcript as the 'turbo' slot.
Run once: .venv/bin/python tests/build_real_case_fixtures.py"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DATA_ROOT = Path(os.environ.get("VERIFYARR_TEST_DATA", "/mnt/c/Users/knham/Desktop/undertekst auto"))
Z = DATA_ROOT / "Z5_flaggede"
SW = DATA_ROOT / "whisper_gpu_staging" / "sweep_linux" / "small.en-greedy-cpu"
OUT = Path(__file__).parent / "fixtures" / "whisper_full"

meta = json.loads((Z / "meta.json").read_text(encoding="utf-8"))
for slug, m in meta.items():
    doc = json.loads((SW / f"{slug}.json").read_text(encoding="utf-8", errors="replace"))
    segs = [{"start": t["offsets"]["from"] / 1000, "end": t["offsets"]["to"] / 1000, "text": t["text"].strip()}
            for t in doc["transcription"] if t["text"].strip()]
    fx = {"slug": slug, "video_name": Path(m["video"]).name, "subtitle_name": Path(m["sub"]).name,
          "duration_seconds": str(m["duration"]), "language": "en", "stt_provider": "local",
          "stt_model": "ggml-small.en.bin", "segment_count": str(len(segs)), "segments": segs}
    (OUT / f"{slug}.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
    print(slug, len(segs))
