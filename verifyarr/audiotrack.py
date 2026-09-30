"""Picks the English audio track of a video, so sync and checks never hear a dub."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from verifyarr.memo import BoundedMemo

_MEMO = BoundedMemo(256)
_ENGLISH_TAGS = ("en", "eng", "english")


_NOT_THE_MAIN_TRACK = ("commentary", "subs", "subtitle", "description")


def _is_english(stream: dict) -> bool:
    """A language tag decides; the title only when the tag is missing or undetermined, and never
    for a commentary / described track."""
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or "").lower()
    if lang and lang != "und":
        return lang in _ENGLISH_TAGS
    title = (tags.get("title") or "").lower()
    return "english" in title and not any(w in title for w in _NOT_THE_MAIN_TRACK)


def english_audio_index(video_path: Path) -> Optional[int]:
    """Index among the audio streams (ffmpeg's 0:a:N) of the first English track;
    None when there is none or ffprobe fails, so callers keep ffmpeg's default."""
    try:
        st = Path(video_path).stat()
        key = (str(video_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key in _MEMO:
        return _MEMO[key]
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index:stream_tags=language,title", "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        streams = json.loads(proc.stdout or "{}").get("streams") or []
    except Exception:
        return None
    idx = next((i for i, s in enumerate(streams) if _is_english(s)), None)
    if key is not None:
        _MEMO.put(key, idx)
    return idx


def audio_map_args(video_path: Path) -> list[str]:
    """ffmpeg output args that select the English track; empty when there is none."""
    idx = english_audio_index(video_path)
    return [] if idx is None else ["-map", f"0:a:{idx}"]
