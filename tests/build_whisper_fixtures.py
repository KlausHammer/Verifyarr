"""Builds tests/fixtures/whisper_full/*.json -- a full-episode Whisper transcript (absolute,
whole-video timestamps) for a handful of real episodes, saved once so every later test run
against them is free.

This is deliberately a standalone SCRIPT, not a `test_*.py` file: it makes real STT API calls
(one per ~10-minute chunk, via generate.transcribe_full_track -- the app's own chunking/silence
-trimming code, reused as-is rather than reinvented) and costs real time and API quota. Nothing
under `tests/` that `unittest discover` picks up should ever do that. Run it once; the fixtures
it writes are what tests/sync_verification.py and tests/test_sync_verification.py actually run
against, offline and free, every time after.

Why a FULL transcript, not just a few spot-checked clips (which is all the app itself ever
pays for): the app's own correctness check only ever sees 3-6 short windows per file, which
audit testing (see the real-Whisper session that motivated this) showed can each be individually
misleading -- one clip landing on a repeated phrase, or on a stretch where the source subtitle
itself has a small pre-existing drift, was enough to flip a sync decision. A transcript of the
WHOLE episode gives a near-continuous timing signal: every subtitle line can be matched against
Whisper's real transcription of the real audio AT THAT POINT, not just the 3-6 points the app
happened to sample. That's what makes it usable as ground truth, and it's also exactly the raw
material tests/sync_verification.py slices back into per-clip pieces to stand in for the app's
own real API calls during a full pipeline run -- so the *pattern* of calls a real sweep makes
(clip-by-clip, at whatever positions collect_samples/correctness_check picks) is preserved and
exercised, just served from this cache instead of the network.

Usage:
    python3 tests/build_whisper_fixtures.py                 # build whatever fixtures are missing
    python3 tests/build_whisper_fixtures.py --force          # rebuild everything
    python3 tests/build_whisper_fixtures.py --episode S02E06 # just one

Requires a real STT API key -- reads verifyarr's own settings DB (same one the app uses) for
sync.groq_api_key/sync.stt_provider, the same credential correctness.py already calls with; a
generate.*-specific key is deliberately NOT required (generate.enabled is off in this install --
see the audit notes -- so generate.groq_api_key was never configured). A throwaway Config copy
below borrows the working sync.* key into the generate.* fields transcribe_full_track reads,
rather than requiring the user to duplicate their key into an unused settings group just for
this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verifyarr import db, generate, log
from verifyarr.correctness import get_duration_seconds
from verifyarr.settings import Config

MEDIA_DIR = Path("/mnt/c/Users/knham/Desktop/undertekst auto/Season 2")
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "whisper_full"

# (slug, video filename, subtitle filename) -- five episodes spread across the season, chosen
# for a real video/subtitle pair each, nothing more special than that. S02E21 doubles as the
# episode the user's own manual testing (see the original audit request) first found the
# "--no-splits" CLI bug against, so it's a nice bonus to have real transcript coverage of it.
EPISODES = [
    ("S02E01", "Community - S02E01 - Anthropology 101 Bluray-1080p Proper.mkv",
     "Community - S02E01 - Anthropology 101 Bluray-1080p Proper.en.hi.srt"),
    ("S02E06", "Community - S02E06 - Epidemiology Bluray-1080p Proper.mkv",
     "Community - S02E06 - Epidemiology Bluray-1080p Proper.en.hi.srt"),
    ("S02E10", "Community - S02E10 - Mixology Certification Bluray-1080p Proper.mkv",
     "Community - S02E10 - Mixology Certification Bluray-1080p Proper.en.hi.srt"),
    # .en.srt, not .en.hi.srt: the .hi track this originally pointed at had its first 16 cues
    # collapsed to 00:00:00,000 (see tests/fixtures/whisper_full/README.md) and has since been
    # replaced on disk by this clean .en track -- kept in sync here so a --force rebuild doesn't
    # regress back to pointing at the broken file.
    ("S02E15", "Community - S02E15 - Early 21st Century Romanticism Bluray-1080p Proper.mkv",
     "Community - S02E15 - Early 21st Century Romanticism Bluray-1080p Proper.en.srt"),
    ("S02E21", "Community - S02E21 - Paradigms of Human Memory Bluray-1080p Proper.mkv",
     "Community - S02E21 - Paradigms of Human Memory Bluray-1080p Proper.en.srt"),
]


def _generate_cfg_with_working_key(cfg: Config) -> Config:
    """A copy of the real Config with generate.*'s STT fields borrowed from sync.*'s (see
    module docstring) -- transcribe_full_track reads cfg.generate_stt_provider/
    active_generate_stt_api_key, not the correctness-check fields collect_samples/
    correctness_check use, even though both ultimately hit the same Groq endpoint."""
    return replace(cfg, generate_stt_provider=cfg.stt_provider, generate_groq_api_key=cfg.groq_api_key,
                   generate_openrouter_api_key=cfg.openrouter_api_key,
                   generate_groq_stt_model=cfg.groq_model,
                   generate_groq_stt_model_fallback=cfg.groq_model_fallback)


def build_one(slug: str, video_name: str, srt_name: str, cfg: Config, force: bool) -> None:
    out_path = FIXTURES_DIR / f"{slug}.json"
    if out_path.exists() and not force:
        log.info("%s: fixture already exists, skipping (--force to rebuild)", slug)
        return

    video_path = MEDIA_DIR / video_name
    srt_path = MEDIA_DIR / srt_name
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not srt_path.exists():
        raise FileNotFoundError(srt_path)

    duration = get_duration_seconds(video_path)
    log.info("%s: transcribing %.0f min of real audio (%s)...", slug, (duration or 0) / 60.0, video_name)

    with tempfile.TemporaryDirectory(prefix=f"whisper-fixture-{slug}-") as td:
        result = generate.transcribe_full_track(cfg, video_path, Path(td), cancel_event=None)

    segments = result["segments"]
    log.info("%s: got %d segment(s), language=%s", slug, len(segments), result["language"])

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "slug": slug,
        "video_name": video_name,
        "subtitle_name": srt_name,
        "duration_seconds": duration,
        "language": result["language"],
        "stt_provider": cfg.stt_provider,
        "stt_model": cfg.groq_model,
        "segment_count": len(segments),
        "segments": segments,
    }, ensure_ascii=False), encoding="utf-8")
    log.info("%s: wrote %s (%d segments, %.0f min)", slug, out_path, len(segments), (duration or 0) / 60.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="rebuild fixtures that already exist")
    ap.add_argument("--episode", help="only build this one (e.g. S02E06)")
    args = ap.parse_args()

    # db.connect() with no path defaults to settings.DEFAULT_DB_PATH (/data/verifyarr.db --
    # the Docker image's fixed path), not this checkout's own data/verifyarr.db -- explicit
    # here the same way every other local/CLI entry point in this repo does it.
    conn = db.connect(Path(__file__).resolve().parent.parent / "data" / "verifyarr.db")
    cfg = _generate_cfg_with_working_key(Config.from_db(conn))
    if not cfg.active_generate_stt_api_key:
        print("No sync.groq_api_key/sync.openrouter_api_key configured in the app's own "
              "settings DB -- nothing to transcribe with.", file=sys.stderr)
        sys.exit(1)

    episodes = [e for e in EPISODES if args.episode is None or e[0] == args.episode]
    if not episodes:
        print(f"No matching episode for --episode {args.episode!r}", file=sys.stderr)
        sys.exit(1)

    for slug, video_name, srt_name in episodes:
        build_one(slug, video_name, srt_name, cfg, args.force)


if __name__ == "__main__":
    main()
