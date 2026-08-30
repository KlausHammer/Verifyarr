"""File discovery (same folder + one level down, e.g. "Subs/")."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from verifyarr import log
from verifyarr.settings import Config

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
LANG_CODE_RE = re.compile(r"^[a-z]{2,3}$")
SXXEYY_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")


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


def build_library_video_rows(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                              all_videos: list[Path]) -> list[dict]:
    """Rows for db.replace_library_videos, derived from a discover_pairs/discover_all_videos
    pair that already exists anyway (from a sweep, or a rescan call) — so persisting the
    Library page never needs another os.walk beyond what already happens."""
    videos_with_subtitle = {video for video, _sub, _lang in pairs}
    rows = []
    for video in all_videos:
        se, title = infer_title_and_episode(video)
        rows.append({
            "video_path": str(video),
            "media_root": str(cfg.media_root_for(video)),
            "kind": cfg.kind_for(video),
            "title": title or str(video.parent),
            "season_episode": se,
            "has_subtitle": video in videos_with_subtitle,
        })
    return rows


def discover_missing(cfg: Config, pairs: list[tuple[Path, Path, Optional[str]]],
                      all_videos: list[Path]) -> list[tuple[Path, str]]:
    """Videos where a wanted language (subtitle_langs) has no subtitle found for it at all —
    used by the sweep to populate 'missing' rows in the files table (see db.mark_missing).
    Only meaningful when subtitle_langs is set — if empty (all languages allowed), 'missing'
    isn't well-defined, so nothing is reported."""
    if not cfg.subtitle_langs:
        return []
    found: dict = {}
    for video, _sub, lang in pairs:
        if lang:
            found.setdefault(video, set()).add(lang)
    missing = []
    for video in all_videos:
        have = found.get(video, set())
        for lang in cfg.subtitle_langs:
            if lang not in have:
                missing.append((video, lang))
    return missing
