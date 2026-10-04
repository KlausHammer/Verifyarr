"""Lowest-priority scheduling for heavy subprocess calls (ffmpeg, alass, local whisper.cpp).
Soft preference, not a cap -- only backs off when something else wants the CPU/disk too."""

from __future__ import annotations

import os
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


_TASKSET_BIN = shutil.which("taskset")


def parse_cpu_list(raw: str | None) -> list[int] | None:
    """"0-3,6" -> [0, 1, 2, 3, 6]; "" -> [] (no restriction); anything malformed -> None."""
    raw = (raw or "").strip()
    if not raw:
        return []
    cpus: set[int] = set()
    for part in raw.split(","):
        lo, dash, hi = part.strip().partition("-")
        if not lo.isdigit() or (dash and not hi.isdigit()):
            return None
        a, b = int(lo), int(hi or lo)
        if b < a:
            return None
        cpus.update(range(a, b + 1))
    return sorted(cpus)


def usable_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def whisper_threads(threads: int, cpus: str | None) -> int:
    """Threads for whisper.cpp: the setting, or (0) every core it is allowed to use."""
    if threads and threads > 0:
        return threads
    listed = parse_cpu_list(cpus)
    return len(listed) if listed else usable_cpus()


def pin_to_cpus(cmd: list[str], cpus: str | None) -> list[str]:
    """Prefixes `cmd` with taskset for the chosen cores; unchanged when none are chosen or taskset is missing."""
    listed = parse_cpu_list(cpus)
    if not listed or not _TASKSET_BIN:
        return list(cmd)
    return [_TASKSET_BIN, "-c", ",".join(map(str, listed)), *cmd]
