"""Live progress inside a file: how many Whisper chunks of each in-flight file are done, so a
run's percentage moves while a long episode is being transcribed (see runs router)."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_chunks: dict[str, list[int]] = {}  # file key -> [done, total]


def start(key: str, total: int) -> None:
    with _lock:
        _chunks[key] = [0, max(1, total)]


def chunk_done(key: str) -> None:
    with _lock:
        if key in _chunks:
            _chunks[key][0] = min(_chunks[key][0] + 1, _chunks[key][1])


def finish(key: str) -> None:
    with _lock:
        _chunks.pop(key, None)


# Listening is only part of a file's work (sync, checks and a possible replacement follow), so the
# chunks fill this share of a file and never the whole of it.
TRANSCRIBE_SHARE = 0.6


def inflight() -> float:
    """Files' worth of work done inside the files that are running now."""
    with _lock:
        return TRANSCRIBE_SHARE * sum(d / t for d, t in _chunks.values())
