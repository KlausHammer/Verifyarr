"""Maintainer tool: Groq whisper-large-v3-turbo transcripts for Known Good episodes, via the app's own
generate.transcribe_full_track (chunking, 429 waits, no fallback model). Reads the API key from the app's
settings database (never printed). Output is the whisper.cpp JSON shape, in whisper_gpu_staging/sweep_linux/groq-turbo/.

    python tests/known_good/groq_transcribe.py KG_PB_S05E01 KG_BOYS_S01E01 KG_SHAM_S05E06
"""
import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
from verifyarr import db, generate
from verifyarr.settings import Config
import build_dataset as B

OUT = B.STAGING / "sweep_linux" / "groq-turbo"
OUT.mkdir(parents=True, exist_ok=True)
APP_DB = Path(__import__("os").environ.get("VERIFYARR_APP_DB", HERE.parent.parent / "data" / "verifyarr.db"))

conn = db.connect(APP_DB)
cfg = Config.from_db(conn)
row = conn.execute("select value from settings where key = ?", ("correctness.groq_api_key",)).fetchone()
cfg = replace(cfg, generate_stt_provider="groq", generate_groq_api_key=(row[0] if row else None),
              generate_groq_stt_model="whisper-large-v3-turbo", generate_groq_stt_model_fallback=None)
assert cfg.active_generate_stt_api_key, "no groq key in the app settings"

for slug in sys.argv[1:] or list(B.EPISODES):
    dst = OUT / f"{slug}.json"
    if dst.exists() and dst.stat().st_size > 1000:
        print(slug, "exists", flush=True)
        continue
    video = B.KNOWN_GOOD / B.EPISODES[slug][1]
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="groq-turbo-") as td:
        res = generate.transcribe_full_track(cfg, video, Path(td), cancel_event=None)
    segs = res["segments"]
    doc = {"model": "groq/whisper-large-v3-turbo", "result": {"language": res["language"]},
           "transcription": [{"offsets": {"from": round(s["start"] * 1000), "to": round(s["end"] * 1000)}, "text": s["text"]}
                             for s in segs]}
    dst.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    print(slug, len(segs), "segments", f"{time.time() - t0:.0f}s", flush=True)
