"""File discovery (same folder + one level down, e.g. "Subs/")."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
from pathlib import Path
from typing import Callable, Optional

from verifyarr import log
from verifyarr.correctness import detect_embedded_subtitle_langs
from verifyarr.settings import Config

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
LANG_CODE_RE = re.compile(r"^[a-z]{2,3}$")
SXXEYY_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")

# ffprobe subprocess calls are I/O-bound (waiting on disk/network, not CPU), so a handful of
# threads in parallel gives a near-linear speedup on a slow/network-mounted library without
# opening so many concurrent handles that a small NAS's disk queue chokes on it.
EMBEDDED_CHECK_WORKERS = 8


def _lang_from_name_parts(name: str, stem_len: int) -> Optional[str]:
    remainder = name[stem_len:]
    parts = [p for p in remainder.split(".") if p]
    if len(parts) >= 2 and LANG_CODE_RE.match(parts[0].lower()):
        return parts[0].lower()
    return None


def _sxxeyy(name: str) -> Optional[str]:
    m = SXXEYY_RE.search(name)
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else None


def infer_title_and_episode(video_path: Path) -> tuple[Optional[str], Optional[str]]:
    """(season_episode, title) best-effort from the filename — e.g. 'S02E01' + 'Community'
    for a series (everything before the SxxEyy pattern, stripped of separators), or
    (None, folder name) when there's no SxxEyy pattern (typically a movie)."""
    m = SXXEYY_RE.search(video_path.name)
    if m:
        se = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        title = video_path.name[:m.start()].strip(" -._")
        return se, (title or None)
    return None, video_path.parent.name


def target_label(video_path: Path) -> str:
    """Human-readable "what got scanned" label for a single-file run's Activity entry, e.g.
    'Community S03E02' for an episode or just the movie's folder name for a movie."""
    se, title = infer_title_and_episode(video_path)
    t = title or video_path.parent.name
    return f"{t} {se}" if se else t


def parse_lang_from_filename(subtitle_path: Path) -> Optional[str]:
    parts = subtitle_path.name.split(".")
    for p in parts[:-1][::-1]:
        if LANG_CODE_RE.match(p.lower()):
            return p.lower()
    return None


def find_subtitles_for_video(video_path: Path, sibling_videos: list[Path]) -> list[tuple[Path, Optional[str]]]:
    """Find subtitles in the same folder, and — if none found there — in a subfolder
    (e.g. 'Subs/', 'Subtitles/') one level down. In a subfolder, either a shared SxxEyy
    pattern with the video is required, or the video must be the only one in its folder
    (movie layout)."""
    stem = video_path.stem
    parent = video_path.parent
    results: list[tuple[Path, Optional[str]]] = []

    try:
        entries = list(parent.iterdir())
    except OSError:
        return results

    # 1) Same folder — requires the filename to start with the video's stem.
    for f in entries:
        if f.is_file() and f.suffix.lower() in SUBTITLE_EXTS and f.name.startswith(stem):
            results.append((f, _lang_from_name_parts(f.name, len(stem))))

    if results:
        return results

    # 2) One folder down.
    ep_tag = _sxxeyy(video_path.name)
    single_video_folder = len(sibling_videos) == 1
    for sub_dir in entries:
        if not sub_dir.is_dir():
            continue
        try:
            sub_entries = list(sub_dir.iterdir())
        except OSError:
            continue
        for f in sub_entries:
            if not f.is_file() or f.suffix.lower() not in SUBTITLE_EXTS:
                continue
            if f.name.startswith(stem):
                results.append((f, _lang_from_name_parts(f.name, len(stem))))
            elif ep_tag and ep_tag.lower() in f.name.lower():
                results.append((f, parse_lang_from_filename(f)))
            elif single_video_folder:
                # Only video in the folder — a subtitle in a subfolder probably belongs to it.
                results.append((f, parse_lang_from_filename(f)))

    return results


def discover_pairs(cfg: Config) -> list[tuple[Path, Path, Optional[str]]]:
    pairs = []
    for root in cfg.media_roots:
        if not root.exists():
            log.warning("Root Folder does not exist: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirpath_p = Path(dirpath)
            if str(dirpath_p).startswith(str(cfg.backup_dir)) or str(dirpath_p).startswith(str(cfg.quarantine_dir)):
                dirnames[:] = []
                continue
            videos = [dirpath_p / n for n in filenames if Path(n).suffix.lower() in cfg.video_exts]
            for video in videos:
                for sub_path, lang in find_subtitles_for_video(video, videos):
                    if cfg.subtitle_langs and lang and lang not in cfg.subtitle_langs:
                        continue
                    pairs.append((video, sub_path, lang))
    return pairs


def discover_all_videos(cfg: Config) -> list[Path]:
    """All video files under the root folders, regardless of whether they have any subtitle
    at all — discover_pairs alone can't be used for this, since a video with NO subtitles
    found doesn't appear in its result. Deliberately walks the tree again (instead of
    collecting this alongside discover_pairs) to keep the two functions independent and
    simple; the cost of an extra os.walk is negligible for a private media library."""
    videos = []
    for root in cfg.media_roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirpath_p = Path(dirpath)
            if str(dirpath_p).startswith(str(cfg.backup_dir)) or str(dirpath_p).startswith(str(cfg.quarantine_dir)):
                dirnames[:] = []
                continue
            videos.extend(dirpath_p / n for n in filenames if Path(n).suffix.lower() in cfg.video_exts)
    return videos


def videos_needing_embedded_check(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                                   all_videos: list[Path], embedded_cache: Optional[dict] = None) -> set[Path]:
    """Every video that build_library_video_rows() and/or discover_missing() would end up
    calling detect_embedded_subtitle_langs() on, computed up front (without any ffprobe calls
    of its own) so a caller can run those checks concurrently or skip them for videos already
    answered another way (see library_poll.refresh_library_cache, which seeds embedded_cache
    from Bazarr's own already-known embedded tracks first, then only threads ffprobe over
    whatever's left)."""
    if embedded_cache is None:
        embedded_cache = {}
    videos_with_subtitle = {video for video, _sub, _lang in pairs}
    found: dict = {}
    for video, _sub, lang in pairs:
        if lang:
            found.setdefault(video, set()).add(lang)
    needs = {video for video in all_videos if video not in videos_with_subtitle}
    if cfg.subtitle_langs:
        for video in all_videos:
            have = found.get(video, set())
            if any(lang not in have for lang in cfg.subtitle_langs):
                needs.add(video)
    return needs - embedded_cache.keys()


def resolve_embedded_cache(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                            all_videos: list[Path], cancel_event: Optional[threading.Event] = None,
                            progress_cb: Optional[Callable[[int, int], None]] = None,
                            extra_cache: Optional[dict] = None,
                            ) -> tuple[dict[Path, set[str]], dict[Path, str]]:
    """(embedded_cache, bazarr_titles) — embedded_cache is {video_path: {embedded lang, ...}}
    for every video that needs one: Bazarr's own already-known embedded tracks first (one bulk
    read, no filesystem access at all), then an 8-worker ffprobe thread pool for whatever
    neither Bazarr nor extra_cache already covers. bazarr_titles is {video_path: title},
    Bazarr's own matched title for whatever it manages — a free byproduct of the same bulk read
    (see bazarr.bazarr_library_info), passed straight through untouched since resolving a title
    needs no filesystem work at all.

    extra_cache: an optional second seed, checked ONLY for a video Bazarr doesn't already cover
    (Bazarr's answer always wins when both have one) — used by the scheduled library poll (see
    library_poll.poll_library_for_new_media -> db.get_persisted_embedded_cache) to skip
    re-ffprobing a video whose size+mtime haven't changed since it was last checked. "Detect
    now" deliberately passes nothing here, so a manual click always re-checks everything fresh.

    Shared by both library_poll.refresh_library_cache (the "Detect now" button and the
    scheduled poll both call this, extra_cache is what tells them apart) and jobs._run_sweep,
    so a sweep gets the same speedup instead of quietly paying for a slow sequential ffprobe
    pass before its very first log line, or its own file loop, even starts — that's what made a
    sweep look hung with zero log output and an unresponsive Cancel for however long the old
    per-video ffprobe fallback took on a large/network-mounted library.

    cancel_event: checked between completed probes (not mid-subprocess) — set means "stop and
    return whatever's resolved so far", never a hard kill of a probe already in flight.
    progress_cb(done, total), if given, is called after each probe completes — used by the
    "Detect now" button's live counter (see library_poll.get_progress)."""
    from verifyarr.bazarr import bazarr_library_info  # local: keeps bazarr.py's own heavier
    # dependency chain (sync_engine/correctness/subtitles/requests) out of every plain
    # discovery.py import, most of which never touch Bazarr at all.

    embedded_cache, bazarr_titles = bazarr_library_info(cfg)
    if extra_cache:
        for video, langs in extra_cache.items():
            embedded_cache.setdefault(video, langs)
    needs = videos_needing_embedded_check(cfg, pairs, all_videos, embedded_cache=embedded_cache)
    total = len(needs)
    if progress_cb:
        progress_cb(0, total)
    if not needs:
        return embedded_cache, bazarr_titles

    done = 0
    cancelled = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=EMBEDDED_CHECK_WORKERS)
    try:
        future_to_video = {pool.submit(detect_embedded_subtitle_langs, v): v for v in needs}
        for future in concurrent.futures.as_completed(future_to_video):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            video = future_to_video[future]
            try:
                embedded_cache[video] = future.result()
            except Exception:
                embedded_cache[video] = set()
            done += 1
            if progress_cb:
                progress_cb(done, total)
    finally:
        # Cancelled: don't block waiting for whatever's still mid-ffprobe -- drop it, those
        # threads finish quietly in the background and their results are simply never used.
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)
    return embedded_cache, bazarr_titles


def build_library_video_rows(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                              all_videos: list[Path],
                              embedded_cache: Optional[dict] = None,
                              bazarr_titles: Optional[dict] = None) -> list[dict]:
    """Rows for db.replace_library_videos, derived from a discover_pairs/discover_all_videos
    pair that already exists anyway (from a sweep, or a rescan call) — so persisting the
    Library page never needs another os.walk beyond what already happens. A video with no
    external subtitle file still counts as having one if it has an embedded subtitle track
    (see discover_missing) — checked lazily, only for videos that need it, to keep this cheap.

    embedded_cache: optional {video: set(langs)}, shared with a discover_missing() call on the
    same all_videos list so a video isn't checked twice. Ideally pre-populated by the caller
    (see library_poll.refresh_library_cache, which seeds it from Bazarr's own already-known
    embedded tracks, then a thread pool for whatever's left) — a video still missing from it
    here falls back to a plain synchronous detect_embedded_subtitle_langs() call, a real
    filesystem read, so on slow/network-mounted media an unseeded cache costs real time.

    bazarr_titles: optional {video: title} — when a video is a key here, its Bazarr-matched
    title wins over the folder/file-name guess (infer_title_and_episode), which is often
    straight-off-a-release-name garbage (see resolve_embedded_cache/bazarr.bazarr_library_info).
    A video Bazarr doesn't manage keeps the guessed title, same as before this existed."""
    if embedded_cache is None:
        embedded_cache = {}
    if bazarr_titles is None:
        bazarr_titles = {}
    videos_with_subtitle = {video for video, _sub, _lang in pairs}
    rows = []
    for video in all_videos:
        se, title = infer_title_and_episode(video)
        title = bazarr_titles.get(video) or title
        has_subtitle = video in videos_with_subtitle
        if not has_subtitle:
            if video not in embedded_cache:
                embedded_cache[video] = detect_embedded_subtitle_langs(video)
            has_subtitle = bool(embedded_cache[video])
        try:
            st = video.stat()
            video_mtime, video_size = st.st_mtime, st.st_size
        except OSError:
            video_mtime = video_size = None
        # None (not "[]") when this video was never embedded-checked at all this round (full
        # external coverage) -- see db.get_persisted_embedded_cache, which only reuses a row
        # that recorded an actual check, never one that skipped it for this reason.
        embedded_langs = embedded_cache.get(video)
        rows.append({
            "video_path": str(video),
            "media_root": str(cfg.media_root_for(video)),
            "kind": cfg.kind_for(video),
            "title": title or str(video.parent),
            "season_episode": se,
            "has_subtitle": has_subtitle,
            "video_mtime": video_mtime,
            "video_size": video_size,
            "embedded_langs_json": json.dumps(sorted(embedded_langs)) if embedded_langs is not None else None,
        })
    return rows


def discover_missing(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                      all_videos: list[Path],
                      embedded_cache: Optional[dict] = None) -> list[tuple[Path, str]]:
    """Videos where a wanted language (subtitle_langs) has no subtitle found for it at all —
    used by the sweep to populate 'missing' rows in the files table (see db.mark_missing).
    Only meaningful when subtitle_langs is set — if empty (all languages allowed), 'missing'
    isn't well-defined, so nothing is reported.

    An embedded subtitle track (baked into the video container, e.g. a bundled MKV track)
    counts as satisfying THAT language only — a video with embedded English but only an
    external Danish file still has Danish flagged/synced normally, and still gets "missing"
    reported for any other wanted language it truly has neither an external file nor an
    embedded track for. Same as Bazarr's own behavior — Bazarr won't fetch a separate file
    for a language it already sees embedded, and this tool has no way to sync/verify an
    embedded track either, so there's nothing to flag or do for that one language. Checked via
    ffprobe, but only for a video that's already missing an external file for that language —
    videos fully covered by external files never pay for the extra probe.

    embedded_cache: see build_library_video_rows — shared so a video isn't checked twice
    across the two functions."""
    if embedded_cache is None:
        embedded_cache = {}
    if not cfg.subtitle_langs:
        return []
    found: dict = {}
    for video, _sub, lang in pairs:
        if lang:
            found.setdefault(video, set()).add(lang)
    missing = []
    for video in all_videos:
        have = found.get(video, set())
        gaps = [lang for lang in cfg.subtitle_langs if lang not in have]
        if not gaps:
            continue
        if video not in embedded_cache:
            embedded_cache[video] = detect_embedded_subtitle_langs(video)
        embedded = embedded_cache[video]
        for lang in gaps:
            if lang not in embedded:
                missing.append((video, lang))
    return missing
