"""Lowest-priority scheduling for heavy subprocess calls (ffmpeg, alass, local whisper.cpp).
Soft preference, not a cap -- only backs off when something else wants the CPU/disk too."""

from __future__ import annotations

import shutil

_NICE_BIN = shutil.which("nice")
_IONICE_BIN = shutil.which("ionice")


def wrap_low_priority(cmd: list[str]) -> list[str]:
    """Prefixes `cmd` with ionice/nice if available; unchanged if neither is installed."""
    prefix: list[str] = []
    if _IONICE_BIN:
        prefix += [_IONICE_BIN, "-c", "3"]  # idle I/O class
    if _NICE_BIN:
        prefix += [_NICE_BIN, "-n", "19"]  # lowest CPU priority
    return [*prefix, *cmd] if prefix else list(cmd)
