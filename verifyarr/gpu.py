"""Does local Whisper find a GPU? Probed once at startup by running whisper.cpp on one second of
silence and reading its Vulkan lines, so the container log and Settings can say so."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from verifyarr import log
from verifyarr.procprio import wrap_low_priority
from verifyarr.settings import Config

_status: dict = {"checked": False, "gpu": False, "detail": "not checked yet"}
_lock = threading.Lock()


def status() -> dict:
    with _lock:
        return dict(_status)


def _set(gpu: bool, detail: str) -> None:
    with _lock:
        _status.update(checked=True, gpu=gpu, detail=detail)
    log.info("Whisper GPU: %s", detail)


def _devices(stderr: str) -> list[str]:
    """Device names from lines like 'ggml_vulkan: 0 = Intel(R) UHD Graphics (...) | uma: 1 | ...'."""
    return [m.group(1).strip() for m in re.finditer(r"ggml_vulkan: \d+ = ([^|\n]+)", stderr)]


def probe(cfg: Config) -> None:
    render_nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    if not cfg.local_whisper_use_gpu:
        return _set(False, "turned off in Settings -> Correctness (CPU only)")
    binary, model = cfg.local_whisper_binary, cfg.local_whisper_model
    if not binary or not Path(binary).is_file() or not model or not Path(model).is_file():
        return _set(False, "not checked (Whisper binary or model missing)")
    with tempfile.TemporaryDirectory(prefix="gpu-probe-") as td:
        wav = Path(td) / "silence.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000)
        try:
            proc = subprocess.run(wrap_low_priority([binary, "-m", model, "-f", str(wav), "-t", "1", "-l", "en",
                                                     "-of", str(Path(td) / "out")]),
                                  capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            return _set(False, f"probe failed ({e})")
    names = _devices(proc.stderr or "")
    if names:
        return _set(True, f"found {', '.join(names)}")
    if not render_nodes:
        return _set(False, "no GPU found: the container has no /dev/dri (add devices: /dev/dri to the compose file) -- running on CPU")
    unreadable = [str(n) for n in render_nodes if not os.access(n, os.R_OK | os.W_OK)]
    if unreadable:
        return _set(False, f"no GPU found: {', '.join(unreadable)} is not accessible to this user (group_add the host's render group) -- running on CPU")
    return _set(False, "no GPU found: /dev/dri is there but Vulkan sees no device -- running on CPU")


def probe_in_background(cfg: Config) -> None:
    threading.Thread(target=lambda: _safe(cfg), name="gpu-probe", daemon=True).start()


def _safe(cfg: Config) -> None:
    try:
        probe(cfg)
    except Exception as e:  # never let a probe break startup
        _set(False, f"probe failed ({e})")
