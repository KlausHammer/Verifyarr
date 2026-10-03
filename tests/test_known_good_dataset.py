"""The packaged Known Good dataset is complete and the matrix runs from it with no audio or video."""
import gzip
import json
import lzma
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "known_good" / "data"
MODELS = ["tiny.en-greedy-cpu", "tiny.en-cpu", "tiny.en-q5_1-cpu", "base.en-greedy-cpu", "base.en-cpu",
          "base.en-q5_1-cpu", "small.en-greedy", "small.en", "small.en-q5_1", "medium.en-greedy", "medium.en",
          "medium.en-q5_0", "turbo-q8_0", "turbo-q5_0", "groq-turbo"]

pytestmark = pytest.mark.skipif(not (DATA / "episodes.json").exists(), reason="Known Good data not present")


def episodes():
    return json.loads((DATA / "episodes.json").read_text(encoding="utf-8"))


def test_ten_verified_episodes_with_subtitle_and_vad():
    eps = episodes()
    assert len(eps) == 10
    for slug, e in eps.items():
        assert e["verified_good"] is True
        assert (DATA / "subtitles" / f"{slug}.srt").stat().st_size > 10_000, slug
        assert (DATA / "vad" / f"{slug}.vad.tsv").stat().st_size > 100, slug
        assert e["duration_seconds"] > 600


def test_every_model_has_a_transcript_for_every_episode():
    missing = [(m, s) for m in MODELS for s in episodes() if not (DATA / "transcripts" / m / f"{s}.json.gz").exists()]
    assert not missing, missing


def test_stored_transcripts_are_usable():
    for m in ("tiny.en-greedy-cpu", "groq-turbo", "medium.en"):
        for s in episodes():
            with gzip.open(DATA / "transcripts" / m / f"{s}.json.gz", "rt", encoding="utf-8") as f:
                doc = json.load(f)
            segs = doc["segments"]
            assert len(segs) > 200 and doc["language"] == "en", (m, s)
            assert all(x["end"] > x["start"] for x in segs[:50])


def test_alass_answers_recorded_for_every_episode():
    for slug in episodes():
        p = DATA / "alass" / f"{slug}.jsonl.xz"
        assert p.exists(), slug
        with lzma.open(p, "rt", encoding="utf-8") as f:
            first = json.loads(next(f))
        assert {"k", "ok", "msg", "tail", "srt"} <= set(first)


def test_matrix_runs_from_the_dataset_without_media(tmp_path):
    env = dict(os.environ, VERIFYARR_KG="replay", VERIFYARR_TEST_DATA=str(tmp_path / "no-media"))
    stem = "kg_pytest_smoke"
    out = ROOT / f"{stem}.jsonl"
    try:
        r = subprocess.run([sys.executable, str(ROOT / "e2e_matrix.py"), "--only", "KG_BOB_S15E01", "--models",
                            "tiny.en-greedy-cpu", "--scenarios", "clean,uniform,drift,missing_middle", "--mode", "full",
                            "--audio-confirm", "off", "--out", stem],
                           env=env, capture_output=True, text=True, timeout=900, cwd=str(ROOT.parent))
        assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
        rows = {x["scenario"]: x for x in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
    finally:
        for p in ROOT.glob(f"{stem}*"):
            p.unlink()
        import shutil
        shutil.rmtree(ROOT / "e2e_work_matrix", ignore_errors=True)
    assert set(rows) == {"clean", "uniform", "drift", "missing_middle"}
    assert all(x["status"] == "ok" for x in rows.values())
    assert rows["clean"]["untouched"] and rows["clean"]["flag"] == "ok"           # no false positive on a verified file
    assert rows["uniform"]["recovered"]["p50"] <= 0.25                             # +45 s fixed
    assert rows["drift"]["recovered"]["p50"] <= 0.25                               # 2 % drift fixed
    assert rows["missing_middle"]["detected"]                                      # flagged, not rewritten
