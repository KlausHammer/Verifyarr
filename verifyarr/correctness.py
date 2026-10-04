"""Correctness check: Whisper transcription + optional translation, compared against the
subtitle's word content. See `correctness_check` for the main flow and scoring logic."""

from __future__ import annotations

import bisect
import math
import json
import statistics
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from verifyarr import log
from verifyarr.audiotrack import audio_map_args, english_audio_index
from verifyarr.memo import BoundedMemo
from verifyarr import db
from verifyarr import vad
from verifyarr.procprio import pin_to_cpus, whisper_threads, wrap_low_priority
from verifyarr.settings import Config, VOCABULARY_HINT_MAX_CHARS
from verifyarr.subtitles import (
    pick_dialogue_dense_time, subs_text_in_window, tokenize, is_nonspeech_annotation, speech_text,
    clip_anchors, anchors_applicable, ANCHOR_SUSPECT_THRESHOLD_S,
)

LANG_CODE_RE = re.compile(r"^[a-z]{2,3}$")

# ISO 639-2/B (ffprobe stream tags) -> ISO 639-1, most common ones only.
LANG3_TO_LANG2 = {
    "eng": "en", "dan": "da", "swe": "sv", "nor": "no", "nob": "no", "nno": "no",
    "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "spa": "es", "ita": "it",
    "nld": "nl", "dut": "nl", "por": "pt", "fin": "fi", "isl": "is", "ice": "is",
    "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "rus": "ru", "pol": "pl",
    "ces": "cs", "cze": "cs",
}


# Stream language tags that carry no usable information — ffprobe reports these when a
# track's language was never set, and treating them as a real language would poison every
# comparison built on them (see _map_lang_tag).
_UNKNOWN_LANG_TAGS = ("und", "unk", "undefined")


def _ffprobe_json(video_path: Path, *extra_args: str, timeout: int) -> dict:
    """Run ffprobe with JSON output, returning {} on ANY failure (missing binary, timeout,
    unparseable output) — every caller here treats "no ffprobe answer" as "unknown", never
    as an error, so there is exactly one place handling the failure modes instead of three
    near-identical try/except blocks."""
    cmd = ["ffprobe", "-v", "error", *extra_args, "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}


def _map_lang_tag(tag: str) -> Optional[str]:
    """A raw ffprobe language tag -> ISO 639-1 code, or None when unusable. ffprobe tags
    are ISO 639-2 ("dan") while the app works in 639-1 ("da") — LANG3_TO_LANG2 covers the
    common ones and anything already shaped like a 639-1/639-2 code passes through."""
    tag = (tag or "").lower()
    if not tag or tag in _UNKNOWN_LANG_TAGS:
        return None
    return LANG3_TO_LANG2.get(tag, tag if LANG_CODE_RE.match(tag) else None)


def detect_audio_language_ffprobe(video_path: Path) -> Optional[str]:
    if english_audio_index(video_path) is not None:
        return "en"  # the English track is what we listen to (see audiotrack)
    data = _ffprobe_json(video_path, "-select_streams", "a:0",
                         "-show_entries", "stream_tags=language", timeout=30)
    streams = data.get("streams", [])
    if not streams:
        return None
    return _map_lang_tag((streams[0].get("tags", {}) or {}).get("language", ""))


def detect_embedded_subtitle_langs(video_path: Path) -> set[str]:
    """Languages of every subtitle track baked into the video container itself, via ffprobe.
    Used so a video isn't flagged 'missing' a language just because there's no separate
    subtitle FILE for it -- Bazarr already considers an embedded track as satisfying that
    language and won't download a separate one either, and this tool has no way to
    sync/verify an embedded track (only external files), so there's nothing to do for it."""
    data = _ffprobe_json(video_path, "-select_streams", "s",
                         "-show_entries", "stream_tags=language", timeout=30)
    langs = set()
    for stream in data.get("streams", []):
        mapped = _map_lang_tag((stream.get("tags", {}) or {}).get("language", ""))
        if mapped:
            langs.add(mapped)
    return langs


_DURATION_MEMO = BoundedMemo(64)


SAMPLED_MIN_CLIPS = 3


def sample_count_for(cfg: Config, duration: Optional[float]) -> int:
    """Sampled clips for a file this long: sync.clips_per_10min (at least 3), or the
    fixed sample_count when that is 0."""
    per10 = getattr(cfg, "clips_per_10min", 0.0) or 0.0
    if per10 > 0 and duration:
        return max(SAMPLED_MIN_CLIPS, math.ceil(duration / 600.0 * per10))
    return max(1, cfg.sample_count)


def get_duration_seconds(video_path: Path) -> Optional[float]:
    """ffprobe duration, remembered per (path, mtime, size): 3-6 callers per file."""
    try:
        st = Path(video_path).stat()
        key = (str(video_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key in _DURATION_MEMO:
        return _DURATION_MEMO[key]
    data = _ffprobe_json(video_path, "-show_entries", "format=duration", timeout=60)
    dur = data.get("format", {}).get("duration")
    try:
        out = float(dur) if dur else None
    except (TypeError, ValueError):
        out = None
    if key is not None and out is not None:
        _DURATION_MEMO.put(key, out)
    return out


# Multipart uploads have to declare what they actually are: correctness.py sends WAV clips,
# generate.py sends compressed MP3 chunks through this same function, and a provider that trusts
# the declared type over the file's own header will reject (or mis-decode) an MP3 announced as
# audio/wav. Kept as an explicit map rather than mimetypes.guess_type, whose answers vary with
# whatever /etc/mime.types the host image happens to ship.
_AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
               ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm"}


def _audio_mime(path: Path) -> str:
    return _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")


def extract_clip(video_path: Path, start_sec: float, duration_sec: int, out_path: Path) -> bool:
    cmd = ["ffmpeg", "-y", "-ss", str(max(0.0, start_sec)), "-t", str(duration_sec),
           "-i", str(video_path), "-vn", *audio_map_args(video_path),
           "-ac", "1", "-ar", "16000", "-f", "wav", str(out_path)]
    try:
        proc = subprocess.run(wrap_low_priority(cmd), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000


# Cloud providers, used by generate.py (subtitle generation) and for translation only -- checks
# never send audio to the cloud. Both have OpenAI-compatible /audio/transcriptions and
# /chat/completions endpoints, so only URL/key/model names change, not the request shape.
_STT_URLS = {
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
    "openrouter": "https://openrouter.ai/api/v1/audio/transcriptions",  # docs: openrouter.ai/docs/guides/overview/multimodal/stt
}
_LLM_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    # Google's OpenAI-compatible endpoint — same request/response shape as the two above (see
    # https://ai.google.dev/gemini-api/docs/openai), added for generate.py's translation step
    # (Settings -> Generate's llm_provider); correctness.py's translate_to_english uses it too.
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


# Module-level pacing shared by every API caller in this app (correctness sampling, line-order
# confirmation, and generate.py's full-track transcription + translation). Safe as a plain
# global because only ONE job can be in flight at a time -- and that is enforced by the database,
# not just by convention: db.create_run inserts against ux_runs_single_running, a unique index on
# status='running', so a second concurrent run raises RunAlreadyActive before it can reach any of
# this. JobRunner's in-process lock is the same rule expressed a second time for the webapp's own
# background thread.
_last_call_at = 0.0


def sleep_cancellable(seconds: float, cancel_event=None) -> None:
    """time.sleep, except a cancel_event set while waiting raises JobCancelled instead of
    sleeping the whole period out. Used by every retry/backoff wait in this app's API plumbing
    (here and in generate.py) -- a plain sleep in a retry loop is a place where Cancel silently
    does nothing for several seconds at a time."""
    if cancel_event is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event.is_set():
            raise JobCancelled("cancelled while waiting to retry")
        time.sleep(min(1.0, remaining))


def _post_ratelimited(url: str, headers: dict, timeout: int, default_wait_s: float = 5,
                       max_wait_s: float = 120, cancel_event=None, fail_fast_on_429: bool = False, **kwargs):
    """POST with two things built in: (1) at least `default_wait_s` between EVERY call (even
    successful ones), to avoid hitting rate limits in the first place, and (2) WAITS and
    retries on an actual rate limit (HTTP 429) instead of failing immediately. Follows the
    provider's own `Retry-After` header when set, falling back to default_wait_s otherwise.
    max_wait_s caps that so an unreasonably long header can't hang the call forever.

    fail_fast_on_429 (default False, unchanged behavior everywhere else): when a fallback
    model actually exists to switch to (see transcribe()/detect_language_and_transcribe()),
    it's faster to raise RateLimitExceeded IMMEDIATELY and let the caller switch models than
    to wait out a whole rate-limit period on a model that's still blocked. Used only for the
    primary STT attempt — LLM translation, Bazarr search, etc. keep the patient behavior.

    cancel_event (optional, threading.Event): checked at short intervals while waiting, so a
    job cancelled from the webapp doesn't have to wait out a whole rate-limit period — only
    used by job-based runs from the webapp, CLI runs don't pass this."""
    global _last_call_at

    def _wait(wait_s: float) -> None:
        sleep_cancellable(wait_s, cancel_event)

    since_last = time.monotonic() - _last_call_at
    if since_last < default_wait_s:
        _wait(default_wait_s - since_last)

    while True:
        resp = requests.post(url, headers=headers, timeout=timeout, **kwargs)
        _last_call_at = time.monotonic()
        if resp.status_code != 429:
            return resp
        if fail_fast_on_429:
            raise RateLimitExceeded(f"rate limited (429): {url}")
        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        try:
            wait_s = min(float(retry_after), max_wait_s)
        except (TypeError, ValueError):
            wait_s = default_wait_s
        log.warning("Rate limit hit — waiting %.0fs before retrying (%s)", wait_s, url)
        _wait(wait_s)


class JobCancelled(Exception):
    """Raised by _post_ratelimited if a cancel_event gets set while waiting for a rate limit
    to clear — caught by jobs.py (the webapp's background job runner)."""


class RateLimitExceeded(Exception):
    """Raised by _post_ratelimited when fail_fast_on_429=True and a 429 response is
    received — see that function's docstring. Not an error on its own, just a signal for
    transcribe()/detect_language_and_transcribe() to switch to the fallback model right away."""


def _transcribe_once(provider: str, audio_path: Path, api_key: str, model: str, *, language: Optional[str],
                      response_format: str, timeout: int = 90, retries: int = 2, cancel_event=None,
                      fail_fast_on_429: bool = False, prompt: Optional[str] = None):
    """prompt: optional free-text vocabulary hint (Whisper's own "prompt" field) -- biases
    recognition toward words/names likely to appear (e.g. a show's character names), which
    helps most with proper nouns and invented words a generic model would otherwise mishear.
    Not used by correctness.py's own callers today; generate.py's full-track transcription
    passes generate.vocabulary_hint through here."""
    url = _STT_URLS[provider]
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": model, "response_format": response_format}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:VOCABULARY_HINT_MAX_CHARS]
    last_err = None
    for attempt in range(retries + 1):
        try:
            log.debug("%s STT request: model=%s lang=%s clip=%s attempt=%d/%d",
                      provider, model, language or "auto", audio_path.name, attempt + 1, retries + 1)
            with open(audio_path, "rb") as f:
                files = {"file": (audio_path.name, f, _audio_mime(audio_path))}
                resp = _post_ratelimited(url, headers, timeout, data=data, files=files, cancel_event=cancel_event,
                                          fail_fast_on_429=fail_fast_on_429)
            log.debug("%s STT response: status=%d clip=%s", provider, resp.status_code, audio_path.name)
            if resp.status_code == 200:
                # "text" is Groq-only — OpenRouter's endpoint 400s on it ("Only json/verbose_json
                # are supported"), so we never send it; "json" works on both and everything else
                # needs is in .text anyway.
                if response_format == "json":
                    return (resp.json().get("text") or "").strip()
                return resp.json()  # verbose_json
            last_err = f"{provider} API error {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < retries:
            sleep_cancellable(2 * (attempt + 1), cancel_event)
    raise RuntimeError(last_err or f"unknown {provider} error")


def _run_cancellable(cmd: list[str], timeout: float, cancel_event=None) -> tuple[int, str, str]:
    """subprocess.run, but a cancel_event set mid-run kills the process and raises JobCancelled
    instead of waiting it out."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise JobCancelled("cancelled while running local Whisper")
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise RuntimeError(f"local Whisper timed out after {timeout:.0f}s")
            time.sleep(0.2)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


def _parse_local_whisper_json(data: dict) -> dict:
    """whisper.cpp's -oj JSON -> the same {"text","language","segments"} shape the cloud
    verbose_json path returns. offsets are ms; segments' start/end here are seconds."""
    segments = []
    parts = []
    for seg in data.get("transcription") or []:
        offsets = seg.get("offsets") or {}
        text = (seg.get("text") or "").strip()
        segments.append({"start": offsets.get("from", 0) / 1000.0,
                          "end": offsets.get("to", 0) / 1000.0, "text": text})
        if text:
            parts.append(text)
    return {"text": " ".join(parts), "language": (data.get("result") or {}).get("language"),
            "segments": segments}


_local_whisper_backend_logged = False


def _log_local_whisper_backend_info(stderr: str) -> None:
    """Logs whisper.cpp's own backend banner once per process -- the easy way to see from
    Activity whether GPU (Vulkan) actually engaged, without shelling into the container."""
    global _local_whisper_backend_logged
    if _local_whisper_backend_logged:
        return
    _local_whisper_backend_logged = True
    for line in stderr.splitlines():
        if line.startswith("system_info:") or "ggml_vulkan" in line:
            log.info("local Whisper backend: %s", line.strip())


# whisper.cpp's own models -- any file the app's own download-ggml-model.sh accepts, e.g.
# "small", "small.en-q5_1", "medium", "large-v3-turbo-q5_0". Only the FILENAME matters here (the
# path's directory is whatever Settings -> Correctness -> "Model file path" says); this just
# guards against firing a request for something that obviously isn't a ggml model filename.
_GGML_MODEL_FILENAME_RE = re.compile(r"^ggml-[\w.\-]+\.bin$")
_WHISPER_MODEL_DOWNLOAD_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{name}"
_whisper_model_download_failed: set[str] = set()


def _download_local_whisper_model(model_path: Path) -> bool:
    """Fetches a missing ggml Whisper model from Hugging Face by filename -- so Settings ->
    Correctness -> "Model file path" (or its default, WHISPER_MODEL in docker-compose) can name
    ANY whisper.cpp model without a Docker rebuild, not just whatever got baked in at build time.
    Tried once per path per process (see _whisper_model_download_failed) -- a real network/name
    failure shouldn't retry on every single clip."""
    key = str(model_path)
    if key in _whisper_model_download_failed:
        return False
    if not _GGML_MODEL_FILENAME_RE.match(model_path.name):
        log.warning("local Whisper model path %r doesn't look like a ggml model filename "
                    "(expected e.g. ggml-small.bin) -- not attempting a download", model_path.name)
        _whisper_model_download_failed.add(key)
        return False

    url = _WHISPER_MODEL_DOWNLOAD_URL.format(name=model_path.name)
    log.info("local Whisper model %s not found -- downloading from %s (one-time, can take a "
             "while)", model_path.name, url)
    tmp_path = model_path.with_name(model_path.name + ".part")
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=60) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        tmp_path.rename(model_path)
    except Exception as e:
        log.warning("Could not download local Whisper model %s: %s", model_path.name, e)
        tmp_path.unlink(missing_ok=True)
        _whisper_model_download_failed.add(key)
        return False
    log.info("local Whisper model %s downloaded (%.0f MB)", model_path.name,
             model_path.stat().st_size / 1_000_000)
    return True


def load_whisper_json(path: Path) -> dict:
    """whisper.cpp's -oj output as a dict, tolerating invalid UTF-8.

    whisper.cpp writes a truncated multibyte sequence now and then -- reproduced with
    base.en-greedy inside a hallucinated song lyric, identically on every run. That raises
    UnicodeDecodeError, which is neither OSError nor JSONDecodeError, so it used to escape
    the caller's handler and fail the whole check over one character. Replacing the bad byte
    costs one mangled character in one segment's text; failing costs the transcription."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise RuntimeError(f"local Whisper produced unreadable output: {e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        log.warning("local Whisper wrote invalid UTF-8 in %s — decoded with replacement "
                    "characters; that segment's text may be slightly mangled", path.name)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"local Whisper produced unparseable output: {e}") from e


def _run_local_whisper(cfg: Config, audio_path: Path, language: Optional[str],
                        cancel_event=None, timeout: Optional[float] = None) -> dict:
    """Runs the local whisper.cpp build instead of a cloud STT call (Settings -> Correctness ->
    "Use local Whisper"). No API key, no rate limit, no fallback model. timeout: bump this for a
    long (full-track) clip -- omitted, it scales with sync.clip_seconds so raising that setting
    can't silently start timing clips out on a slow CPU-only box."""
    timeout = timeout if timeout is not None else max(300.0, cfg.clip_seconds * 10.0)
    binary, model = cfg.local_whisper_binary, cfg.local_whisper_model
    if not binary or not Path(binary).is_file():
        raise RuntimeError(f"local Whisper binary not found: {binary!r} (see Settings -> Correctness)")
    if model and not Path(model).is_file():
        _download_local_whisper_model(Path(model))
    if not model or not Path(model).is_file():
        raise RuntimeError(f"local Whisper model not found: {model!r} (see Settings -> Correctness). "
                           f"A ggml-*.bin file is downloaded automatically on first use, so a missing "
                           f"file usually means the box is offline or the path is wrong")

    with tempfile.TemporaryDirectory(prefix="local-whisper-") as td:
        out_stem = Path(td) / "out"
        # An .en model has no language-detection head at all, so "auto" there silently means
        # "assume English" -- make that explicit rather than letting a Danish track come back
        # tagged "en" and pass the require_audio_lang gate on a guess.
        lang = language or ("en" if ".en" in Path(model).name else "auto")
        # No -nt: that disables whisper's own timestamp decoding, not just console output.
        # -mc 0 (carry no text context between windows) prevents a severe decoder repetition
        # loop -- measured on v1.9.4, the version the Dockerfile pins, on a 10-minute chunk:
        # without it, 164 consecutive copies of one lyric covering 109-321s; with it, one.
        # Costs a little on a build that doesn't have the loop (~2% of segments in a short
        # repeat on one Windows/GPU build), which is far the cheaper side of that trade --
        # the loop otherwise silently deletes minutes of dialogue from the evidence.
        # generate._drop_repetition_loops still catches whatever gets through.
        # Greedy decoding (-bs 1 -bo 1): the sweep measured every model both ways, and greedy
        # was both faster and better on the worst file (medium.en 0.873 greedy vs 0.820 beam,
        # 72s vs 135s). whisper-cli defaults to beam 5.
        cmd = [binary, "-m", model, "-f", str(audio_path), "-oj", "-of", str(out_stem),
               "-t", str(max(1, whisper_threads(cfg.local_whisper_threads, getattr(cfg, "local_whisper_cpus", "")))), "-l", lang, "-mc", "0",
               "-bs", "1", "-bo", "1"]
        if not cfg.local_whisper_use_gpu:
            cmd.append("-ng")
        log.debug("local Whisper: model=%s lang=%s clip=%s", Path(model).name, language or "auto",
                  audio_path.name)
        returncode, _stdout, stderr = _run_cancellable(pin_to_cpus(wrap_low_priority(cmd), getattr(cfg, "local_whisper_cpus", "")), timeout=timeout,
                                                        cancel_event=cancel_event)
        if returncode != 0:
            raise RuntimeError(f"local Whisper failed (exit {returncode}): {stderr[-500:]}")
        _log_local_whisper_backend_info(stderr)
        out_path = out_stem.with_suffix(".json")
        if not out_path.exists():
            raise RuntimeError(f"local Whisper produced no output file: {stderr[-300:]}")
        data = load_whisper_json(out_path)
    return _parse_local_whisper_json(data)


def transcribe_verbose(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    """Same call as transcribe(), but Whisper's full verbose_json response: {"text", "language",
    "segments": [{"start","end","text"}, ...]}. Same request, same price -- the segment timing
    is what makes the per-clip anchors (subtitles.clip_anchor_shift) possible, so every clip
    this module transcribes uses this shape and caches the segments (db.save_transcript_cache)
    for whoever checks the same video next. language=None -> Whisper detects it.

    Local whisper.cpp only -- cloud speech recognition is for generating subtitles."""
    return _run_local_whisper(cfg, audio_path, language, cancel_event=cancel_event)


def translate_text(text: str, target_lang: str, *, provider: str, api_key: str, llm_model: str,
                    llm_model_fallback: Optional[str] = None, system_prompt: Optional[str] = None,
                    max_chars: int = 2000, cancel_event=None) -> Optional[str]:
    """Generic LLM-based translation of one piece of subtitle text to `target_lang` (a plain
    language name, e.g. "English" or "Danish" -- goes straight into the default prompt, not
    looked up against any code table). Provider/key/model are all passed in explicitly rather
    than read off a Config, so this one function serves BOTH correctness.py's
    translate_to_english (below, a thin wrapper) and generate.py's translate_segments/
    _translate_batch, which each resolve their own (possibly different) provider/model settings
    first. Tries `llm_model_fallback` on failure, same as before this was generalized.

    system_prompt: overrides the default single-line instruction below -- used by
    generate.py's batch translation, which needs the model to preserve a numbered-line
    structure across multiple subtitle lines in one call rather than translate one string.

    max_chars: truncation limit for the user content -- the default (2000) is sized for
    correctness.py's own short comparison-window text; generate.py's batch translation passes a
    much higher limit and sizes its batches to stay under it (see generate._iter_batches), since
    a truncated numbered list would silently drop trailing lines and break _translate_batch's own
    count/order validation. Truncation is logged rather than silent for exactly that reason."""
    if not text.strip():
        return ""
    if len(text) > max_chars:
        log.warning("Truncating %d characters of text down to %d before translating — the "
                    "translation will be missing whatever came after that point",
                    len(text), max_chars)
    url = _LLM_URLS[provider]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    fallback_model = llm_model_fallback or None
    prompt = system_prompt or (f"Translate the user's subtitle text to {target_lang}. "
                                "Output ONLY the translation, no notes or quotes.")

    models_to_try = [llm_model] + ([fallback_model] if fallback_model and fallback_model != llm_model else [])
    for i, model in enumerate(models_to_try):
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text[:max_chars]},
            ],
        }
        if "gpt-oss" in model:
            # gpt-oss models are reasoning models that can otherwise burn the ENTIRE
            # completion-token budget on invisible "thinking" and return 0 chars of output
            # (finish_reason=length) for text this size. "low" keeps thinking short enough
            # that the translation actually gets written. Only gpt-oss supports this
            # parameter — other models (e.g. allam) return 400 if it's sent.
            payload["reasoning_effort"] = "low"
        try:
            resp = _post_ratelimited(url, headers, 30, json=payload, cancel_event=cancel_event)
            if resp.status_code != 200:
                is_last = i == len(models_to_try) - 1
                log.warning("%s translation failed (%s) with model %s%s: %s", provider, resp.status_code, model,
                            "" if is_last else " — trying fallback model", resp.text[:200])
                continue
            # Defensive rather than indexed straight through: a provider can answer 200 with a
            # content-filter/empty completion (content: null, or no choices at all), and letting
            # that surface as an AttributeError from .strip() hides what actually happened behind
            # a generic exception line.
            choices = (resp.json() or {}).get("choices") or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
            if not content:
                is_last = i == len(models_to_try) - 1
                log.warning("%s translation returned no content with model %s%s", provider, model,
                            "" if is_last else " — trying fallback model")
                continue
            return content.strip()
        except JobCancelled:
            raise
        except Exception as e:
            log.warning("%s translation failed with model %s: %s", provider, model, e)
            continue
    return None


# translate_to_english results, keyed on (provider, model, text) -- the same subtitle window
# text gets translated over and over within one run: once for the primary sync candidate's own
# correctness check, then once more per alternative candidate pipeline._resolve_ambiguous_sync
# scores against the SAME cached transcripts (a candidate shifted by less than the comparison
# window has a byte-identical window text). Those repeats were real, paid LLM calls for nothing.
# Bounded and process-local: a few hundred short strings, oldest dropped first.
_TRANSLATION_MEMO: dict[tuple[str, str, str], str] = {}
_TRANSLATION_MEMO_MAX = 512


def translate_to_english(cfg: Config, text: str, cancel_event=None) -> Optional[str]:
    """Thin wrapper around translate_text, resolving correctness.*'s own provider/model settings
    -- unchanged behavior/call sites (line_order.py, _compare_transcript_to_window above) from
    before translate_text was extracted out of this function -- plus a memo of successful
    results (see _TRANSLATION_MEMO), so re-scoring the same window text costs nothing."""
    kw = cfg.llm_call_kwargs
    if not kw["api_key"]:
        log.warning("No %s API key under Settings -> Generate: cannot translate a subtitle for the check",
                    kw["provider"])
        return None
    key = (kw["provider"], kw["llm_model"], text)
    hit = _TRANSLATION_MEMO.get(key)
    if hit is not None:
        return hit
    translated = translate_text(text, "English", cancel_event=cancel_event, **kw)
    if translated is not None:
        if len(_TRANSLATION_MEMO) >= _TRANSLATION_MEMO_MAX:
            _TRANSLATION_MEMO.pop(next(iter(_TRANSLATION_MEMO)))
        _TRANSLATION_MEMO[key] = translated
    return translated


def _compare_transcript_to_window(cfg: Config, transcript: str, window_text: str, sub_lang: Optional[str],
                                   transcript_lang: Optional[str], cancel_event=None) -> dict:
    """The actual "does this transcript match what the subtitle claims is said here" scoring —
    translates the subtitle window text to English first if the transcript is English but the
    subtitle isn't (translation direction/limits unchanged from before this was extracted).
    Shared by correctness_check's own samples AND line_order.check_subtitle's samples (some of
    which are heuristic-anchored clips instead of correctness_check's usual spread-out picks) —
    kept as one function so the actual comparison math never drifts between the two callers.
    Returns {"transcript_excerpt", "score"} or {"error"} (translation failure only)."""
    compare_text = window_text
    if sub_lang and transcript_lang and sub_lang != transcript_lang:
        translated = translate_to_english(cfg, window_text, cancel_event=cancel_event) \
            if transcript_lang == "en" else None
        if translated is None and transcript_lang == "en":
            return {"error": "subtitle translation failed"}
        compare_text = translated if translated is not None else window_text
    t_tokens, w_tokens = tokenize(transcript), tokenize(compare_text)
    score = (len(t_tokens & w_tokens) / len(t_tokens)) if t_tokens else None
    return {"transcript_excerpt": transcript[:160], "score": round(score, 3) if score is not None else None,
            "sub_tokens": len(w_tokens)}


def _aggregate_correctness(samples: list[dict], cfg: Config) -> tuple[Optional[float], str]:
    """avg_score + ok/SUSPECT flag from a list of {"score": float|None, ...} samples — shared by
    correctness_check and line_order.check_subtitle so the SUSPECT decision (majority of samples
    must individually clear the threshold, not just the average — see below) is identical either
    way regardless of how the samples were chosen."""
    valid = [s["score"] for s in samples if s.get("score") is not None]
    avg = sum(valid) / len(valid) if valid else None
    if avg is None:
        return None, "unknown (no valid samples)"
    # Require a MAJORITY of individual samples to clear the threshold on their own, not just
    # the average. A plain average lets one randomly high-scoring sample (e.g. shared
    # character names/universe vocabulary between episodes of the same series — the generic
    # stopword filter in tokenize() doesn't catch that) drag an otherwise wrong subtitle over
    # the threshold. Verified against a known mismatched file: 0/3 samples passed
    # individually, vs. 2/3 for a known-correct file.
    passing = sum(1 for sc in valid if sc >= cfg.overlap_threshold)
    return avg, ("ok" if passing * 2 >= len(valid) else "SUSPECT")


# How many samples must show a significant residual before the file is escalated. One or two is
# not evidence: measured over 52 real episodes, every false escalation (5 files) had 1-2 flagged
# samples and every true one (9 files) had 3-14, so this separates them exactly. Magnitude does
# not -- a real 3.4s residual and a false 4.8s one both occur.
ANCHOR_SUSPECT_MIN_SAMPLES = 3


def significant_anchor_residuals(samples: list[dict],
                                 threshold: float = ANCHOR_SUSPECT_THRESHOLD_S,
                                 min_samples: int = ANCHOR_SUSPECT_MIN_SAMPLES) -> list[dict]:
    """Samples whose Whisper-anchor shift (subtitles.clip_anchor_shift — a content-VERIFIED
    point estimate of the real timing offset at that exact instant, not the bag-of-words window
    score) exceeds `threshold` seconds (default subtitles.ANCHOR_SUSPECT_THRESHOLD_S -- see
    there for why it is NOT sync.min_change_seconds). A confident anchor (one that survived the
    median/MAD agreement check) showing a real residual mismatch is independent evidence of a
    problem AT THAT SPECIFIC POINT even when the whole-file average/majority-vote already
    passed — see Config.anchor_check_enabled for the motivating case (a subtitle that's only
    right for part of the episode).

    Fewer than min_samples of them is not evidence -- see ANCHOR_SUSPECT_MIN_SAMPLES."""
    bad = [s for s in samples if s.get("anchor") and abs(s["anchor"]["shift"]) > threshold]
    return bad if len(bad) >= min_samples else []


# Speech plays in one direction, so a correct subtitle maps audio time to cue time with slope 1.
# Between two anchors that is |shift2 - shift1| / (t2 - t1) away from 1 -- a STEP, i.e. a block
# boundary the fix did not resolve. It needs no missing evidence: a half-repaired file anchors
# as densely as a healthy one (measured: 76% vs 77% of samples), so absence proves nothing and
# this reads the anchors we do have.
# Both bars must be cleared, and the second is what makes it safe. The slope ceiling alone is a
# property of the subtitle's OWN local timing errors, not a noise floor (measured: one episode's
# maximum sat on the same anchor pair in 13 of 13 scenarios), so it travels badly to new files.
# The absolute step does not: healthy files top out at 3.3s (matrix) and 2.0s (52 real episodes)
# while unresolved blocks sit at 11-23s. 2s through 5s catch exactly the same rows, so 5 is
# chosen for margin, not fit. Measured: 0 false positives on 542 healthy/correctly-fixed matrix
# rows and 60 real healthy rows (incl. the 12 Slow Horses the user confirms are correct).
SLOPE_BREAK_MIN_DEV = 0.30
SLOPE_BREAK_MIN_STEP_S = 5.0


def anchor_slope_breaks(samples: list[dict],
                        min_dev: float = SLOPE_BREAK_MIN_DEV,
                        min_step: float = SLOPE_BREAK_MIN_STEP_S) -> list[dict]:
    """Consecutive anchor pairs whose audio->cue mapping steps instead of running at slope 1.

    Returns [{"at", "dev", "step"}, ...] in time order. Empty when fewer than two confident
    anchors exist -- an undefined slope is not a verdict (a wrong-episode subtitle typically
    anchors nothing at all, and that is the content check's job, not this one)."""
    pts = sorted((s["start"], s["anchor"]["shift"]) for s in samples
                 if s.get("anchor") and s.get("start") is not None)
    out = []
    for (t1, s1), (t2, s2) in zip(pts, pts[1:]):
        if t2 <= t1:
            continue
        step = abs(s2 - s1)
        dev = step / (t2 - t1)
        if dev > min_dev and step > min_step:
            out.append({"at": t2, "dev": round(dev, 2), "step": round(s2 - s1, 1)})
    return out


# Per-cue noise: each anchor's own MAD, judged on full coverage only. Measured on correct
# SH files over 15 models: full peaks at 0.295s; sampled medians over 5-6 anchors reach
# 0.65s (turbo) -- so sampled only buys the full transcript. Jitter caught at 0.56s.
JITTER_MIN_MAD_S = 0.5
JITTER_MIN_ANCHORS = 5
# Sampled trigger: below the verdict bar, it only buys the look.
JITTER_ESCALATE_MAD_S = 0.4


def anchor_jitter(samples: list[dict], min_anchors: int = JITTER_MIN_ANCHORS) -> Optional[float]:
    """Median of the anchors' own MAD, or None under min_anchors.

    Random per-cue timing has no offset to find: every clip median averages it away, so the
    anchors' shifts look healthy. It survives only inside each anchor, as disagreement
    between that clip's own lines."""
    mads = [s["anchor"]["mad"] for s in samples
            if s.get("anchor") and s["anchor"].get("mad") is not None]
    return statistics.median(mads) if len(mads) >= min_anchors else None


# A missing middle (or head/tail): a cue gap holding dialogue. Music-tagged
# segments (♪/♫) don't count. Healthy SH gaps across 15 models + the turbo
# out/ transcripts peak at 86 words (turbo-q5_0; tiny.en-greedy 34): bigger
# models transcribe background TV/radio the subtitle rightly skips. 100 words
# clears every model; skips of >= 10 lines are caught 91-98% (tiny.en-greedy
# 98%), >= ~20 lines 100%. Speech density does NOT separate across models
# (turbo's long segments put real skips at 0.7-1.3 words/s).
# tiny (greedy, as shipped) gets 50: healthy SH peaks at 34, 16-line skips
# carry 71; 52 real episodes on tiny: 50 and 100 flag the same files.
MISSING_MIDDLE_MIN_GAP_S = 20.0
MISSING_MIDDLE_MIN_SPEECH_S = 15.0
MISSING_MIDDLE_MIN_WORDS = 100
MISSING_MIDDLE_MIN_WORDS_TINY = 50


def missing_middle_min_words(model: Optional[str]) -> int:
    return MISSING_MIDDLE_MIN_WORDS_TINY if "tiny" in (model or "").lower() \
        else MISSING_MIDDLE_MIN_WORDS


def clears_missing_middle(secs: float, words: int, min_words: int) -> bool:
    """One gap's speech clears both bars."""
    return secs >= MISSING_MIDDLE_MIN_SPEECH_S and words >= min_words


# Sampled mode transcribes only the gaps, not the file: every bare gap >= 50s
# (60s holes leave >= 56.8s bare), whole, in clip-sized pieces, stopping once the
# bar is met. Sampling part of a gap does not work: healthy SH gaps reach 1.2
# words/s in places (untitled speech at E03 5:39, every model) while a tiny probe
# of a real 300s hole read 0.96 -- only the whole-gap count separates (86 vs 100).
# Cost on SH: 4-6 min of audio per episode vs 45-60 for the full transcript.
MISSING_MIDDLE_ESCALATE_GAP_S = 50.0


def gap_probe_windows(g0: float, g1: float, clip_s: float) -> list[tuple[float, float]]:
    """Back-to-back (start, duration) clips covering one gap."""
    out, t = [], g0
    while t < g1 - 0.5:
        out.append((t, min(clip_s, g1 - t)))
        t += clip_s
    return out


def cue_gaps(subs, min_gap_s: float = MISSING_MIDDLE_MIN_GAP_S) -> list[tuple[float, float]]:
    """(start, end) seconds of every between-cues hole >= min_gap_s."""
    ev = sorted(subs.events, key=lambda e: e.start)
    out, max_end = [], None
    for e in ev:
        if max_end is not None and (e.start - max_end) / 1000.0 >= min_gap_s:
            out.append((max_end / 1000.0, e.start / 1000.0))
        max_end = e.end if max_end is None else max(max_end, e.end)
    return out


# Opening songs and recaps (The White Lotus S01E06 0:00-2:20), outro songs, credits and
# promos for other shows (The Boys S05E07, Marvelous Mrs. Maisel S01E07, What We Do in the
# Shadows S03E02, S.W.A.T. S03E18): nothing is judged in the first and last two minutes.
EDGE_IGNORE_S = 120.0


def all_gaps(subs, duration_s: Optional[float],
             min_gap_s: float = MISSING_MIDDLE_MIN_GAP_S) -> list[tuple[float, float]]:
    """cue_gaps plus head [0, first cue] and tail [last cue, duration], all clipped to
    [EDGE_IGNORE_S, duration - EDGE_IGNORE_S] and dropped when shorter than min_gap_s then."""
    gaps = list(cue_gaps(subs, min_gap_s))
    if not subs.events:
        return gaps
    ev = sorted(subs.events, key=lambda e: e.start)
    if ev[0].start / 1000.0 >= min_gap_s:
        gaps.append((0.0, ev[0].start / 1000.0))
    if duration_s:
        gaps.append((max(e.end for e in ev) / 1000.0, duration_s))
    # Ten percent at most, so a short file still has a middle to judge.
    edge = min(EDGE_IGNORE_S, 0.1 * duration_s) if duration_s else EDGE_IGNORE_S
    lo, hi = edge, (duration_s - edge) if duration_s else float("inf")
    return [(max(x, lo), min(y, hi)) for x, y in gaps if min(y, hi) - max(x, lo) >= min_gap_s]


def _is_music(text: str) -> bool:
    return "♪" in text or "♫" in text


MUSIC_TAG_RE = re.compile(r"\b(?:music|song|singing|sings)\b", re.IGNORECASE)
MUSIC_MARGIN_S = 8.0
# Marks further apart than this are separate songs, each with its own window.
# Forgiving on purpose: nothing says where a song really starts and ends, and a
# false "missing lines" costs a new subtitle. FROM S04E03: [MUSIC] 7:06 ...
# (upbeat music) 8:56 with lyrics between; MINDHUNTER S01E01 speech over music
# with the subtitles burned into the video.
MUSIC_CLUSTER_GAP_S = 150.0
# The lyrics outlast the last mark: Westworld S01E01's end-credit song ran 38s past
# its last mark. Applied after the last mark of a cluster only.
MUSIC_TRAIL_S = 45.0


def music_spans(segments: list[dict]) -> list[tuple[float, float]]:
    """(start, end) of raw transcript segments marked as music: ♪, or a bracket-only
    tag naming music ("(upbeat music)")."""
    out = []
    for s in segments or []:
        t = s.get("text") or ""
        if _is_music(t) or (is_nonspeech_annotation(t) and MUSIC_TAG_RE.search(t)):
            try:
                out.append((float(s["start"]), float(s["end"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def gap_speech(segments: list[dict], g0: float, g1: float,
               music: Optional[list[tuple[float, float]]] = None) -> tuple[float, int]:
    """(overlap seconds, words) of transcript segments in [g0, g1). Words count
    segments starting inside (one utterance, one vote); seconds count overlap.
    Music-tagged segments don't count -- a song is not a missing scene. With `music`
    spans, everything from the first to the last mark inside the gap (+margin) is
    ignored too: tiny writes some lyrics without a mark, turbo writes all of them
    bare, so only the speech outside the song can show a missing scene."""
    windows: list[list[float]] = []
    for m in sorted(m for m in music or [] if m[1] > g0 and m[0] < g1):
        if windows and m[0] - windows[-1][1] <= MUSIC_CLUSTER_GAP_S:
            windows[-1][1] = max(windows[-1][1], m[1])
        else:
            windows.append([m[0], m[1]])
    windows = [(lo - MUSIC_MARGIN_S, hi + MUSIC_TRAIL_S) for lo, hi in windows]
    secs, words = 0.0, 0
    for s in segments:
        try:
            st, en = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if en <= st or _is_music(s.get("text") or ""):
            continue
        if any(st < hi and en > lo for lo, hi in windows):
            continue
        if min(en, g1) - max(st, g0) > 0:
            secs += min(en, g1) - max(st, g0)
        if g0 <= st < g1:
            words += len(speech_text(s.get("text") or "").split())
    return secs, words


# A gap the subtitle itself opens with a music description ("[MICK HARVEY'S "OUT
# OF TIME, MAN" PLAYS]", "[♪♪♪]") is a song, not missing dialogue: tiny writes
# the lyrics without ♪ (Breaking Bad S01E01 55:01, 60 words in 43s). Whole-cue
# descriptions only -- lyric lines are subtitled speech. Capped: a song, not an act.
MUSIC_CUE_RE = re.compile(
    r"^\s*[\[(][^\])]*(?:\b(?:PLAYS|PLAYING|MUSIC|SONG|SINGING|SINGS)\b|♪)[^\])]*[\])]\s*$",
    re.IGNORECASE)
DECLARED_MUSIC_MAX_S = 240.0


def declared_music_gap(subs, g0: float, g1: float) -> bool:
    """True when the cue the gap opens after is a music description and the gap
    is song-sized."""
    if g1 - g0 > DECLARED_MUSIC_MAX_S:
        return False
    return any(abs(e.end / 1000.0 - g0) < 0.001 and MUSIC_CUE_RE.match(e.plaintext)
               for e in subs.events)


# Speech after the audio ends cannot be right; says only THAT something is wrong
# (cut version, drift, offset, wrong file). Healthy SH+KG end 1-78s early.
OVERRUN_TOLERANCE_S = 1.0


def overrun_evidence(subs, duration_s: Optional[float]) -> Optional[dict]:
    """Speech cues starting after the audio ends: {"n", "over_s"}, or None."""
    if not duration_s or subs is None:
        return None
    past = [e for e in subs.events if e.start / 1000.0 > duration_s + OVERRUN_TOLERANCE_S
            and speech_text(e.text).strip()]
    if not past:
        return None
    return {"n": len(past), "over_s": round(max(e.end for e in past) / 1000.0 - duration_s, 1)}


def missing_middle_evidence(subs, segments: list[dict],
                            duration_s: Optional[float] = None,
                            min_words: Optional[int] = None,
                            music: Optional[list[tuple[float, float]]] = None) -> Optional[dict]:
    """Loudest cue gap clearing both speech bars, or None. Segments must already
    carry full_transcript_for_check's own filters (nonspeech + repetition loops):
    unfiltered, one turbo loop hallucinated 653 words into a healthy gap.
    duration_s adds head/tail gaps (truncated downloads)."""
    best = None
    min_words = MISSING_MIDDLE_MIN_WORDS if min_words is None else min_words
    gaps = all_gaps(subs, duration_s) if duration_s else cue_gaps(subs)
    for g0, g1 in gaps:
        if declared_music_gap(subs, g0, g1):
            continue
        secs, words = gap_speech(segments, g0, g1, music)
        if clears_missing_middle(secs, words, min_words) \
                and (best is None or secs > best["speech_s"]):
            best = {"gap_start": g0, "gap_end": g1, "speech_s": secs, "words": words}
    return best


# A block the clip anchors miss: raw matched lines (one per cue, not clip
# medians -- sparse dialogue leaves most clips under 3 lines) that run
# together at one offset: >= 4 of k in a row, tight, off the file.
# tiny.en-greedy full transcript: healthy SH peaks at 1.43s (k=6, MAD<=0.5);
# injected blocks 90-600s x 2.3-20s sit at 2.64s and up (24/24). Model-bound:
# other models run 2-12s on healthy SH (collapsed timestamps), see 14.33.
POINT_RUN_K = 6
POINT_RUN_MIN_DEV_S = 2.0
POINT_RUN_MAX_MAD_S = 0.5


def anchor_point_runs(samples: list[dict], k: int = POINT_RUN_K,
                      min_dev: float = POINT_RUN_MIN_DEV_S,
                      max_mad: float = POINT_RUN_MAX_MAD_S) -> list[dict]:
    """Stretches where k consecutive matched lines share one offset away from the
    file's: [{"from","to","dev","n","cue_from","cue_to"}], merged. from/to are audio
    times, cue_from/cue_to the run's own cue times. Uses anchor_points (audio, cue)."""
    pts: dict[float, float] = {}
    for s in samples or []:
        for a, c in s.get("anchor_points") or []:
            pts[float(a)] = float(a) - float(c)
    seq = sorted(pts.items())
    if len(seq) < k:
        return []
    ref = statistics.median(sh for _, sh in seq)
    runs: list[dict] = []
    for i in range(len(seq) - k + 1):
        win = [sh - ref for _, sh in seq[i:i + k]]
        m = statistics.median(win)
        if abs(m) < min_dev or statistics.median(abs(x - m) for x in win) > max_mad:
            continue
        lo, hi = seq[i][0], seq[i + k - 1][0]
        cues = [a - sh for a, sh in seq[i:i + k]]
        if runs and lo <= runs[-1]["to"] and (runs[-1]["dev"] > 0) == (m > 0):
            r = runs[-1]
            r["to"], r["n"] = hi, r["n"] + 1
            r["dev"] = m if abs(m) > abs(r["dev"]) else r["dev"]
            r["cue_from"], r["cue_to"] = min(r["cue_from"], *cues), max(r["cue_to"], *cues)
        else:
            runs.append({"from": lo, "to": hi, "dev": round(m, 2), "n": k,
                         "cue_from": min(cues), "cue_to": max(cues)})
    for r in runs:
        r["dev"] = round(r["dev"], 2)
    return runs


def dense_anchor_points(subs, segments: list[dict], clip_s: float = 30.0,
                        before_s: float = 30.0, after_s: float = 60.0) -> list[dict]:
    """Every matched line over back-to-back clips of a full transcript, as
    samples for anchor_point_runs. Production clips sit ~60s apart and miss
    most lines of a short block (E04 0:30-2:50: 3 of 9)."""
    from verifyarr.subtitles import _match_segments_to_lines
    segs = sorted(segments, key=lambda s: float(s.get("start") or 0.0))
    starts = [float(s.get("start") or 0.0) for s in segs]
    end = max((float(s.get("end") or 0.0) for s in segs), default=0.0)
    out, t = [], 0.0
    while t < end:
        clip = segs[bisect.bisect_left(starts, t):bisect.bisect_left(starts, t + clip_s)]
        if clip:
            m = _match_segments_to_lines(clip, 0.0, subs, t - before_s, t + clip_s + after_s)
            if m:
                out.append({"start": t, "anchor_points":
                            [(a["segment_start_abs"], a["matched_line_start_sec"]) for a in m]})
        t += clip_s
    return out


# A block: >=2 anchors >2.5s off the file median within 5 minutes, or one >5s.
# Healthy SH (tiny.en-greedy) peaks at 1.96s deviation, so 2.5s clears with
# margin; a 180s block holds 2-4 anchors. Scattered singles are Whisper noise.
BLOCK_CLUSTER_MIN_ANCHORS = 2
BLOCK_CLUSTER_DEV_S = 2.5
BLOCK_CLUSTER_WINDOW_S = 300.0
BLOCK_SINGLE_S = 5.0


def anchor_block_clusters(samples: list[dict], dev_s: float = BLOCK_CLUSTER_DEV_S,
                          single_s: float = BLOCK_SINGLE_S,
                          window_s: float = BLOCK_CLUSTER_WINDOW_S) -> list[list[dict]]:
    """Groups of off-median anchors sharing a 5-minute window (block shape).

    Returns the had samples per cluster (for resync/notes), [] when clean.
    Absolute shift is not used: a uniform offset moves every anchor together."""
    pts = sorted((s["start"], s["anchor"]["shift"], s) for s in samples
                 if s.get("anchor") and s.get("start") is not None)
    if len(pts) < 2:
        return []
    ref = statistics.median(sh for _, sh, _ in pts)
    bad = [(t, sh, s) for t, sh, s in pts if abs(sh - ref) > dev_s]
    huge = [[s] for _, sh, s in pts if abs(sh - ref) > single_s]
    if huge:
        return huge
    out, cur = [], []
    for t, _sh, s in bad:
        if cur and t - cur[0][0] > window_s:
            if len(cur) >= BLOCK_CLUSTER_MIN_ANCHORS:
                out.append([s for _, _, s in cur])
            cur = []
        cur.append((t, _sh, s))
    if len(cur) >= BLOCK_CLUSTER_MIN_ANCHORS:
        out.append([s for _, _, s in cur])
    return out


# A block remainder: k consecutive anchors whose median sits min_dev off the file's.
# Measured on full-mode matrix rows: correct unflagged files peak at 0.86s (k=10),
# the three silent half-repaired ones sit at 1.54-2.14s.
ANCHOR_RUN_K = 10
ANCHOR_RUN_MIN_DEV_S = 1.2


def anchor_run_offsets(samples: list[dict], k: int = ANCHOR_RUN_K,
                       min_dev: float = ANCHOR_RUN_MIN_DEV_S) -> list[dict]:
    """Stretches of k consecutive anchors that agree with each other but not with the file.

    A repair that settled most of a block leaves its remainder 1.5-2.5s off -- under every
    per-anchor threshold, and without a step big enough for anchor_slope_breaks. Jitter
    averages out over k anchors; a remainder keeps its sign. Empty under k anchors."""
    pts = sorted((s["start"], s["anchor"]["shift"]) for s in samples
                 if s.get("anchor") and s.get("start") is not None)
    if len(pts) < k:
        return []
    ref = statistics.median(sh for _, sh in pts)
    out = []
    for i in range(len(pts) - k + 1):
        dev = statistics.median(sh for _, sh in pts[i:i + k]) - ref
        if abs(dev) < min_dev:
            continue
        if out and pts[i][0] <= out[-1]["to"]:
            out[-1]["to"] = pts[i + k - 1][0]
            if abs(dev) > abs(out[-1]["dev"]):
                out[-1]["dev"] = round(dev, 1)
        else:
            out.append({"from": pts[i][0], "to": pts[i + k - 1][0], "dev": round(dev, 1)})
    return out


def evaluate_against_cached_transcripts(conn, video_path: Path, subs: "pysubs2.SSAFile",
                                        sub_lang: Optional[str], transcript_lang: Optional[str],
                                        cfg: Config, *, score: bool = True,
                                        cancel_event=None) -> Optional[dict]:
    """Judges `subs` against EVERY Whisper transcript already cached for this video
    (video_transcript_cache -- audio-only, content-independent of any particular subtitle, see
    db.get_cached_transcripts_for_video) -- the n evenly-spread slots AND any block-targeted
    extra slots, from this run or any earlier one, any language. No new Whisper calls. Used to
    judge the sync candidates in pipeline._resolve_ambiguous_sync (the original, pre-sync
    subtitle; alass's single-offset fit; its multi-block fit) on ONE common evidence set, so
    they're compared like for like rather than each on whatever samples it happened to get.

    Two kinds of evidence per cached clip, same sample shape line_order.collect_samples
    produces ({"start", "score", "transcript_excerpt", "anchor"}):
      - "anchor": subtitles.clip_anchor_shift over the cached segments -- the candidate's
        TIMING residual at that clip, content-verified line by line. Free (CPU only), and the
        only evidence that can tell two candidates with the same text but different timing
        apart: the window score below cannot see a shift smaller than the comparison window
        (+/-window_minutes) at all. None when the row has no segments (saved before segments
        existed), or when the subtitle isn't in the spoken language (subtitles.
        anchors_applicable) -- "no timing evidence", never "timing confirmed".
      - "score" (only when score=True): the ordinary word-overlap window score -- the
        "is this even the right episode's text" question. NOT free for a subtitle in another
        language than the audio (each window gets an LLM translation, memoised per text --
        see translate_to_english), which is why callers ask for anchors alone first and only
        pay for scores when the anchors can't settle it.

    Returns {"avg_score", "flag", "samples"} (avg_score/flag None when score=False), or None if
    nothing at all is cached -- "not enough data to compare", not a pass or a fail."""
    stt_provider, stt_model = full_transcript_cache_key(cfg)
    rows = db.get_cached_transcripts_for_video(conn, video_path, stt_provider=stt_provider,
                                               stt_model=stt_model)
    if not rows:
        return None
    window_before = cfg.window_minutes * 60
    use_anchors = anchors_applicable(sub_lang, transcript_lang)
    samples = []
    for r in rows:
        start = r["start"]
        # The window has to cover the whole clip that was transcribed -- a heuristic line-order
        # cluster clip can be far longer than sync.clip_seconds, and judging its transcript
        # against a too-short window would score every candidate low for no reason.
        window_after = (r.get("clip_seconds") or cfg.clip_seconds) + cfg.window_minutes * 60
        anchor = None
        anchor_pts: list = []
        if use_anchors and r.get("segments"):
            # defensive: rows saved before the nonspeech-annotation filter may still carry them
            clean = [s for s in r["segments"] if not is_nonspeech_annotation(s.get("text", ""))]
            anchor, anchor_pts = clip_anchors(clean, start, subs, start - window_before,
                                              start + window_after)
        sample = {"start": start, "anchor": anchor, "anchor_points": anchor_pts}
        if score:
            window_text = subs_text_in_window(subs, start, window_before, window_after)
            compare = _compare_transcript_to_window(cfg, r["transcript"], window_text, sub_lang,
                                                     transcript_lang, cancel_event=cancel_event)
            sample.update(compare)
        samples.append(sample)
    if not score:
        return {"avg_score": None, "flag": None, "samples": samples}
    scorable = [s for s in samples if "error" not in s]
    if not scorable:
        return None
    avg, flag = _aggregate_correctness(scorable, cfg)
    return {"avg_score": avg, "flag": flag, "samples": scorable}


# Fixed interval for evaluate_against_full_transcript's ANCHOR-only pass (score=False) -- free
# (no LLM/API call, same-language token overlap only, see subtitles.clip_anchor_shift), so it
# stays at this interval regardless of file length rather than being diluted on a long movie --
# matches line_order.FULL_MODE_ANCHOR_INTERVAL_S.
ANCHOR_PASS_INTERVAL_S = 60.0
# Cap on how many windows the SCORED pass (score=True) slices out -- each one can cost an LLM
# translation call for a foreign-language subtitle (see _compare_transcript_to_window), so THIS
# pass bounds a long movie's worst case rather than sampling every clip_seconds all the way through.
MAX_FULL_ANCHOR_WINDOWS = 60


# Anchor interval for the RESYNC planner specifically (pipeline._try_anchor_resync), finer than
# ANCHOR_PASS_INTERVAL_S. Anchors cost no API call at all -- just token overlap against a
# transcript already in hand -- so when the question changes from "is this file wrong" to "by how
# much, where", it is worth paying CPU for three times the resolution. Measured on the real
# multi-block files: at 60s C_S03E05's four offset changes are backed by 1-2 anchors each and the
# file has to be refused; at 20s the same file resolves into five regions of 3-13 anchors.
ANCHOR_RESYNC_INTERVAL_S = 20.0


class WhisperCost:
    """Audio seconds actually sent to Whisper, and how many came free from a cache.

    Raw audio seconds alone overstate the cost: the per-video clip cache and the full-track
    cache are both keyed on the video, so a file checked twice pays once. Both numbers are
    needed to answer "what does a run cost" -- the first is the bill, the second is what the
    cache saved. Module-level because the transcription points sit three call layers below the
    pipeline; safe because correctness/line-order runs one file at a time on one thread (see
    correctness_and_finish)."""

    def __init__(self):
        self.fresh_s = 0.0
        self.cached_s = 0.0

    def reset(self) -> None:
        self.fresh_s = self.cached_s = 0.0

    def snapshot(self) -> dict:
        return {"fresh_audio_s": round(self.fresh_s, 1), "cached_audio_s": round(self.cached_s, 1)}


whisper_cost = WhisperCost()


# Re-exported from db (where it lives so vad.py can use it too) -- the (provider,
# model) pair BOTH transcript caches are keyed on; see db.full_transcript_cache_key.
full_transcript_cache_key = db.full_transcript_cache_key


def evaluate_against_full_transcript(conn, video_path: Path, subs: "pysubs2.SSAFile",
                                     sub_lang: Optional[str], transcript_lang: Optional[str],
                                     cfg: Config, *, score: bool = True,
                                     anchor_interval: Optional[float] = None,
                                     cancel_event=None) -> Optional[dict]:
    """sync.whisper_mode == "full" counterpart to evaluate_against_cached_transcripts -- sourced
    from the full-track transcript cache (video_full_transcript_cache, see
    generate.full_transcript_for_check) instead of the per-clip cache, so a sync candidate is
    judged against the WHOLE file's dialogue rather than a handful of clips. No new Whisper
    calls: by the time pipeline._resolve_ambiguous_sync calls this, line_order.
    collect_samples_full has already populated (or reused) this cache entry earlier in the same
    run. Returns None if nothing is cached yet for this (video, provider, model)."""
    provider, model = full_transcript_cache_key(cfg)
    cached = db.get_full_transcript_cache(conn, video_path, stt_provider=provider, stt_model=model)
    if cached is None or not cached["segments"]:
        return None
    # The cache stores what transcribe_full_track produced, tags and all: generate's own
    # subtitle-creation feature shares that cache and may legitimately want "[music]" kept,
    # so generate._drop_nonspeech runs on the way OUT of full_transcript_for_check, not on
    # the way in. Reading the cache directly means doing that filtering here -- otherwise an
    # anchor can match a cue against "(screaming)" and call it dialogue. The clip-based twin
    # (evaluate_against_cached_transcripts) has filtered all along; this path had not.
    segments = [s for s in cached["segments"] if not is_nonspeech_annotation(s.get("text", ""))]
    if not segments:
        return None

    window_before = cfg.window_minutes * 60
    window_after = cfg.clip_seconds + cfg.window_minutes * 60
    use_anchors = anchors_applicable(sub_lang, transcript_lang)
    duration = max((s["end"] for s in segments), default=0.0)
    # score=False (the free first pass) can afford the dense fixed interval; score=True (only run
    # for candidates the free pass didn't already confirm) bounds LLM-translation cost instead.
    step = (anchor_interval or ANCHOR_PASS_INTERVAL_S) if not score else max(
        float(cfg.clip_seconds), duration / MAX_FULL_ANCHOR_WINDOWS if duration else 30.0)

    samples = []
    t = 0.0
    while t < duration:
        clip_segs = [s for s in segments if t <= s["start"] < t + cfg.clip_seconds]
        if clip_segs:
            anchor, anchor_pts = (clip_anchors(clip_segs, 0.0, subs, t - window_before, t + window_after)
                                  if use_anchors else (None, []))
            sample = {"start": round(t, 1), "anchor": anchor, "anchor_points": anchor_pts}
            if score:
                window_text = subs_text_in_window(subs, t, window_before, window_after)
                transcript = " ".join(s.get("text", "") for s in clip_segs)
                compare = _compare_transcript_to_window(cfg, transcript, window_text, sub_lang,
                                                         transcript_lang, cancel_event=cancel_event)
                sample.update(compare)
            samples.append(sample)
        t += step

    if not score:
        return {"avg_score": None, "flag": None, "samples": samples}
    scorable = [s for s in samples if "error" not in s]
    if not scorable:
        return None
    avg, flag = _aggregate_correctness(scorable, cfg)
    return {"avg_score": avg, "flag": flag, "samples": scorable}


def correctness_check(video_path: Path, subs: "pysubs2.SSAFile", sub_lang: Optional[str],
                       cfg: Config, tmp_dir: Path, conn=None, cancel_event=None) -> dict:
    """Standalone correctness check, isolated to one file — used by bazarr.py's
    verify_subtitle_candidate to vet a replacement subtitle before adopting it. The main
    sweep/scan flow (pipeline.process_pair) does NOT call this: it always goes through
    line_order.collect_samples()/finalize_line_order() instead, so a correctness check also
    collects (and caches, across runs) line-order candidate data at no extra cost — see
    line_order.py's module docstring.

    conn: optional sqlite3 connection — when given, each of the n sample slots is looked up in
    video_transcript_cache (keyed on video + slot index + STT provider/model, not subtitle)
    before calling Whisper,
    and saved there after a fresh call. The audio doesn't change with the subtitle, so this
    benefits ANY later check of the same video: a remediation candidate tried right after
    another (see bazarr.verify_subtitle_candidate), a different language, or a normal Scan of
    a since-replaced subtitle. Omit (None) to always transcribe fresh, e.g. a one-off caller
    with no DB handle."""
    duration = get_duration_seconds(video_path)
    if not duration:
        return {"skipped": True, "reason": "could not read duration (ffprobe)"}

    log.info("Whisper: local (whisper.cpp, %s)", Path(cfg.local_whisper_model).name)

    audio_lang = detect_audio_language_ffprobe(video_path)
    if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
        # ffprobe's language tag alone is enough to decide this — skip sampling entirely.
        reason = f"speech is '{audio_lang}' (per the file's metadata), not '{cfg.require_audio_lang}' — skipped"
        return {"skipped": True, "reason": reason}

    # Clip count scales with length (sample_count_for: 2 per 10 min). duration is split
    # into that many equal regions (spread evenly across the whole file), and in each
    # region the most dialogue-dense clip start (pick_dialogue_dense_time) is picked instead
    # of a blind timestamp — avoids landing a sample on a silent or action-heavy stretch.
    n = sample_count_for(cfg, duration)
    regions = [(duration * i / n, duration * (i + 1) / n) for i in range(n)]
    transcript_lang = audio_lang or cfg.require_audio_lang

    # Speech timeline for VAD-guided placement (free when a prior check of this video --
    # any subtitle -- cached segments, else an optional Silero run, else None). None keeps
    # today's dialogue-density behavior bit-for-bit (see vad.pick_sample_time).
    timeline, timeline_whole = vad.timeline_for_video(conn, video_path, cfg)
    stt_provider, stt_model = full_transcript_cache_key(cfg)
    built = []
    for idx, (region_start, region_end) in enumerate(regions):
        # The audio at a given point in this video doesn't depend on which subtitle is being
        # checked against it -- ANY earlier check of THIS video (a remediation candidate, a
        # different language, an earlier normal Scan, ...) may already have transcribed this
        # region's slot; reuse it and skip both pick_dialogue_dense_time and Whisper entirely.
        cached = (db.get_cached_transcript(conn, video_path, idx, within=(region_start, region_end),
                                           stt_provider=stt_provider, stt_model=stt_model)
                  if conn is not None else None)
        if cached is not None:
            start = cached["start"]
            transcript = cached["transcript"]
            # filter again in case this row predates the nonspeech-annotation filter below
            segments = [s for s in (cached.get("segments") or []) if not is_nonspeech_annotation(s.get("text", ""))]
            if audio_lang is None:
                audio_lang = cached["audio_lang"]
                transcript_lang = audio_lang or cfg.require_audio_lang
        else:
            base = pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)
            start = vad.pick_sample_time(subs, timeline, region_start, region_end,
                                         cfg.clip_seconds, base, cfg.vad_min_speech_seconds,
                                         whole_file=timeline_whole)
            if start is None:
                # Proven silence: record non-evidence, spend no STT call (same {"start",
                # "error"} shape extraction failures already produce -- flows through below
                # and carries no anchor, i.e. no timing evidence either way).
                built.append({"start": round(base, 1),
                              "error": f"VAD silence-skip (<{cfg.vad_min_speech_seconds:g}s speech in window)"})
                continue
            clip_path = tmp_dir / f"clip_{int(start)}.wav"
            if not extract_clip(video_path, start, cfg.clip_seconds, clip_path):
                built.append({"start": round(start, 1), "error": "audio extraction failed"})
                continue
            try:
                # verbose_json every time (same call, same price as plain text): the segment
                # timing is what the anchors below and any later check of this video's cache
                # need. Language: known -> sent; unknown on the first sample (neither ffprobe
                # tags nor filename) -> Whisper guesses, reused for the rest of the file.
                known = None if (audio_lang is None and idx == 0) else (transcript_lang or "en")
                result = transcribe_verbose(cfg, clip_path, known, cancel_event=cancel_event)
            except Exception as e:
                built.append({"start": round(start, 1), "error": str(e)})
                continue
            finally:
                clip_path.unlink(missing_ok=True)
            segments = result.get("segments") or []
            transcript = (result.get("text") or " ".join(s.get("text", "") for s in segments)).strip()
            if audio_lang is None and known is None:
                audio_lang = result.get("language")
                transcript_lang = audio_lang
            # Sound-effect/audio-condition tags ("[screaming]", "(music)") tokenize as ordinary
            # words and can spuriously match real dialogue -- strip before caching, so every
            # later reader of this cache (this loop next time, get_cached_transcripts_for_video)
            # sees a clean evidence set too. `transcript` (the LLM check's own text) keeps them.
            segments = [s for s in segments if not is_nonspeech_annotation(s.get("text", ""))]
            if conn is not None:
                db.save_transcript_cache(conn, video_path, idx, start, audio_lang, transcript, segments=segments,
                                         clip_seconds=cfg.clip_seconds,
                                         stt_provider=stt_provider, stt_model=stt_model)

        if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
            reason = f"speech is '{audio_lang}', not '{cfg.require_audio_lang}' — skipped"
            return {"skipped": True, "reason": reason}

        built.append({"start": round(start, 1), "transcript": transcript, "segments": segments,
                      "clip_start": start})

    samples = []
    for item in built:
        start = item["start"]
        if "error" in item:
            samples.append({"start": start, "error": item["error"]})
            continue
        transcript = item["transcript"]

        window_before = cfg.window_minutes * 60
        window_after = cfg.clip_seconds + cfg.window_minutes * 60
        window_text = subs_text_in_window(subs, start, window_before, window_after)
        anchor = None
        anchor_pts: list = []
        if item.get("segments") and anchors_applicable(sub_lang, transcript_lang):
            anchor, anchor_pts = clip_anchors(item["segments"], item["clip_start"], subs,
                                              item["clip_start"] - window_before,
                                              item["clip_start"] + window_after)
        compare = _compare_transcript_to_window(cfg, transcript, window_text, sub_lang, transcript_lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": start, "error": compare["error"], "anchor": anchor,
                            "anchor_points": anchor_pts})
            continue
        samples.append({"start": start, "anchor": anchor, "anchor_points": anchor_pts, **compare})

    avg, flag = _aggregate_correctness(samples, cfg)
    return {"skipped": False, "avg_score": avg, "samples": samples, "flag": flag, "audio_lang": audio_lang}
