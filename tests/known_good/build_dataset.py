"""Maintainer tool: packs the Known Good episodes into tests/known_good/data/ (no audio, no video).

Needs the local media once (Known Good folder, the whisper_gpu_staging sweeps, the VAD sidecars).
Everyone else just runs the tests against data/ -- see README.md.

    python tests/known_good/build_dataset.py            # all episodes found
    VERIFYARR_TEST_DATA="/path/to/undertekst auto" python tests/known_good/build_dataset.py
"""
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ROOT = Path(os.environ.get("VERIFYARR_TEST_DATA", "/mnt/c/Users/knham/Desktop/undertekst auto"))
STAGING = ROOT / "whisper_gpu_staging"
KNOWN_GOOD = ROOT / "Known Good"

# slug -> (series, video file in Known Good). The user verified every one of these as correct.
EPISODES = {
    "KG_BB_S01E01": ("Breaking Bad", "Breaking Bad - S01E01 - Pilot Bluray-1080p.mp4"),
    "KG_BIL_S01E01": ("Billions", "Billions - S01 E01 - Pilot (720p HDTV).mkv"),
    "KG_BMS_S01E01": ("Blue Mountain State", "Blue Mountain State - S01E01 - It's Called Hazing, Look It Up Bluray-1080p.mkv"),
    "KG_BOB_S15E01": ("Bob's Burgers", "Bob's Burgers - S15E01 - The Tina Table - The Tables Have Tina-Ed WEBDL-1080p.mp4"),
    "KG_EUP_S01E01": ("Euphoria (US)", "Euphoria (US) - S01E01 - Pilot WEBDL-1080p Proper.mkv"),
    "KG_PB_S05E01": ("Peaky Blinders", "Peaky Blinders - S05E01 - Black Tuesday Bluray-1080p.mp4"),
    "KG_SHAM_S05E06": ("Shameless (US)", "Shameless (US) - S05E06 - Crazy Love Bluray-1080p.mp4"),
    "KG_BOYS_S01E01": ("The Boys", "The Boys - S01E01 - The Name of the Game Bluray-1080p.mp4"),
    "SH_S01E01": ("Slow Horses", "Slow Horses - S01E01 - Failure's Contagious WEBDL-1080p.mkv"),
    "SH_S01E06": ("Slow Horses", "Slow Horses - S01E06 - Follies WEBDL-1080p.mkv"),
}

# matrix model name -> directories to look in (first hit wins) and the file stem used there
MODELS = {
    "tiny.en-cpu": ["tiny.en-cpu", "tiny.en"],
    "tiny.en-greedy-cpu": ["tiny.en-greedy-cpu", "tiny.en-greedy"],
    "tiny.en-q5_1-cpu": ["tiny.en-q5_1-cpu", "tiny.en-q5_1"],
    "base.en-cpu": ["base.en-cpu", "base.en"],
    "base.en-greedy-cpu": ["base.en-greedy-cpu", "base.en-greedy"],
    "base.en-q5_1-cpu": ["base.en-q5_1-cpu", "base.en-q5_1"],
    "small.en": ["small.en"],
    "small.en-greedy": ["small.en-greedy"],
    "small.en-q5_1": ["small.en-q5_1"],
    "medium.en": ["medium.en"],
    "medium.en-greedy": ["medium.en-greedy"],
    "medium.en-q5_0": ["medium.en-q5_0"],
    "turbo-q5_0": ["turbo-q5_0"],
    "turbo-q8_0": ["turbo-q8_0"],
    "groq-turbo": ["groq-turbo"],
}
SEARCH = ["sweep", "sweep_linux", "kg_sweep_gpu"]   # under whisper_gpu_staging


def segments_of(doc):
    segs = []
    for t in doc.get("transcription") or []:
        off = t.get("offsets") or {}
        try:
            a, b = float(off["from"]) / 1000.0, float(off["to"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        text = (t.get("text") or "").strip()
        if text and b > a:
            segs.append({"start": round(a, 3), "end": round(b, 3), "text": text})
    return segs


def load_sweep(path):
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("utf-8", errors="replace"))


def wav_seconds(wav):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                         capture_output=True, text=True)
    return round(float(out.stdout.strip()), 2)


def main():
    DATA.mkdir(exist_ok=True)
    (DATA / "subtitles").mkdir(exist_ok=True)
    (DATA / "vad").mkdir(exist_ok=True)
    episodes, missing = {}, []
    for slug, (series, video) in EPISODES.items():
        stem = video.rsplit(".", 1)[0]
        subs = sorted(KNOWN_GOOD.glob(glob_escape(stem) + ".en*.srt"))
        wav = STAGING / "wav" / f"{slug}.wav"
        if not subs or not wav.exists():
            missing.append(slug)
            continue
        shutil.copyfile(subs[0], DATA / "subtitles" / f"{slug}.srt")
        vad = STAGING / "out" / f"{slug}.vad.tsv"
        if vad.exists():
            shutil.copyfile(vad, DATA / "vad" / f"{slug}.vad.tsv")
        episodes[slug] = {"series": series, "video_name": video, "subtitle_source": subs[0].name,
                          "hearing_impaired": ".en.hi." in subs[0].name,
                          "duration_seconds": wav_seconds(wav), "language": "en", "verified_good": True}
    n_tr = 0
    for model, dirs in MODELS.items():
        for slug in episodes:
            src = next((p for base in SEARCH for d in dirs
                        for p in [STAGING / base / d / f"{slug}.json"] if p.exists()), None)
            if src is None:
                continue
            doc = load_sweep(src)
            segs = segments_of(doc)
            if not segs:
                continue
            lang = (doc.get("result") or {}).get("language") or (doc.get("params") or {}).get("language") or "en"
            out = DATA / "transcripts" / model / f"{slug}.json.gz"
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {"slug": slug, "model": model, "language": lang,
                       "source": str(src.relative_to(STAGING)), "segments": segs}
            with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            n_tr += 1
    (DATA / "episodes.json").write_text(json.dumps(episodes, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(episodes)} episodes, {n_tr} transcripts; missing media/subtitle: {missing or 'none'}")


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


if __name__ == "__main__":
    sys.exit(main())
