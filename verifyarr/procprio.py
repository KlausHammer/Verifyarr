"""Lowest-priority scheduling for this app's heavy CPU/IO subprocess calls (ffmpeg extraction,
alass, local whisper.cpp) -- every one of them is background batch work with no latency
requirement of its own, unlike whatever else a shared box might be doing at the same moment (a
NAS's file-sharing/streaming duties, another app, a person actually using the machine). This
isn't a Settings toggle -- it's always on, the same way none of these calls have ever needed a
"go easy on resources" switch before; there's no real reason anyone would want this tool to win a
resource fight it doesn't need to win.

wrap_low_priority() prepends `ionice -c3` (idle I/O class -- only gets disk time nothing else
currently wants) and `nice -n19` (lowest CPU scheduling priority) to a command, best-effort: if
either binary isn't on PATH (both are standard on Debian/Ubuntu -- ionice from util-linux, nice
from coreutils, both Essential/Priority:required packages so any Debian-family image has them --
but this stays defensive for a stripped-down base image or a non-Linux dev machine), that half is
silently skipped rather than failing the whole command.

Important: this is a SOFT preference, not a hard cap. Linux's CFS scheduler and IO schedulers
that support IO priority classes (e.g. BFQ; some, like plain mq-deadline on NVMe, ignore IO
priority entirely) only apply it under actual contention -- on an otherwise-idle box this app
still gets the full CPU/IO it asks for, exactly as before. It only steps back when something else
genuinely wants the resource, which is the whole point: never slow anything else down, never slow
itself down for no reason either. Nice/ionice values set inside a container are real host
scheduling decisions (containers share the host kernel's scheduler, this isn't namespaced), so
this applies whether verifyarr runs standalone or as one of several containers/services sharing a
box (e.g. other apps on the same TrueNAS/Unraid host) -- for that multi-container case, pair this
with docker-compose.yml's cpu_shares, which does the equivalent thing at the whole-container
level so verifyarr's container as a whole also yields CPU to other containers under contention."""

from __future__ import annotations

import shutil

_NICE_BIN = shutil.which("nice")
_IONICE_BIN = shutil.which("ionice")


def wrap_low_priority(cmd: list[str]) -> list[str]:
    """Returns `cmd` prefixed with whatever of ionice/nice is available -- unmodified if neither
    is. Order matters: ionice needs to be the outermost exec so it can set the calling process's
    (not yet execed) IO priority class before nice execs again and finally reaches `cmd` itself;
    each wrapper's scheduling attribute is inherited across exec, so the final process ends up
    with both applied regardless of which one actually runs first."""
    prefix: list[str] = []
    if _IONICE_BIN:
        prefix += [_IONICE_BIN, "-c", "3"]
    if _NICE_BIN:
        prefix += [_NICE_BIN, "-n", "19"]
    return [*prefix, *cmd] if prefix else list(cmd)
