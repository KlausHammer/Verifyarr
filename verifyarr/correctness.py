"""Correctness check: Whisper transcription + optional translation, compared against the
subtitle's word content. See `correctness_check` for the main flow and scoring logic."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from verifyarr import log
from verifyarr import db
from verifyarr import vad
from verifyarr.procprio import wrap_low_priority
from verifyarr.settings import Config, VOCABULARY_HINT_MAX_CHARS
from verifyarr.subtitles import (
    pick_dialogue_dense_time, subs_text_in_window, tokenize, is_nonspeech_annotation,
    clip_anchor_shift, clip_anchors, anchors_applicable, ANCHOR_SUSPECT_THRESHOLD_S,
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


def get_duration_seconds(video_path: Path) -> Optional[float]:
    data = _ffprobe_json(video_path, "-show_entries", "format=duration", timeout=60)
    dur = data.get("format", {}).get("duration")
    try:
        return float(dur) if dur else None
    except (TypeError, ValueError):
        return None


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
           "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_path)]
    try:
        proc = subprocess.run(wrap_low_priority(cmd), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000


# Two providers are supported for BOTH transcription and translation — Config.stt_provider
# picks which (see Settings -> Correctness). Both have OpenAI-compatible /audio/transcriptions
# and /chat/completions endpoints, so only URL/key/model names change, not the request shape.
_STT_URLS = {
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
    "openrouter": "https://openrouter.ai/api/v1/audio/transcriptions",  # docs: openrouter.ai/docs/guides/overview/multimodal/stt
}
_LLM_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    # Google's OpenAI-compatible endpoint — same request/response shape as the two above (see
    # https://ai.google.dev/gemini-api/docs/openai), added for generate.py's translation step
    # (Settings -> Generate's llm_provider). Not used by correctness.py's own translate_to_english.
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


def _stt_model_and_fallback(cfg: Config) -> tuple[str, Optional[str]]:
    if cfg.stt_provider == "openrouter":
        return cfg.openrouter_stt_model, (cfg.openrouter_stt_model_fallback or None)
    return cfg.groq_model, (cfg.groq_model_fallback or None)


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
        raise RuntimeError(f"local Whisper model not found: {model!r} (see Settings -> Correctness)")

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
               "-t", str(max(1, cfg.local_whisper_threads)), "-l", lang, "-mc", "0",
               "-bs", "1", "-bo", "1"]
        if not cfg.local_whisper_use_gpu:
            cmd.append("-ng")
        log.debug("local Whisper: model=%s lang=%s clip=%s", Path(model).name, language or "auto",
                  audio_path.name)
        returncode, _stdout, stderr = _run_cancellable(wrap_low_priority(cmd), timeout=timeout,
                                                        cancel_event=cancel_event)
        if returncode != 0:
            raise RuntimeError(f"local Whisper failed (exit {returncode}): {stderr[-500:]}")
        _log_local_whisper_backend_info(stderr)
        out_path = out_stem.with_suffix(".json")
        if not out_path.exists():
            raise RuntimeError(f"local Whisper produced no output file: {stderr[-300:]}")
        data = load_whisper_json(out_path)
    return _parse_local_whisper_json(data)


def _transcribe_with_fallback(cfg: Config, audio_path: Path, language: Optional[str],
                              response_format: str, cancel_event=None):
    """Primary STT model, then the configured fallback model if that fails -- switching
    immediately (without waiting out the rate-limit period) if the failure was specifically a
    429 and a fallback actually exists (see _post_ratelimited's fail_fast_on_429). language=None
    lets Whisper detect the spoken language itself (verbose_json reports it back)."""
    model, fallback = _stt_model_and_fallback(cfg)
    api_key = cfg.active_stt_api_key
    try:
        return _transcribe_once(cfg.stt_provider, audio_path, api_key, model, language=language,
                                response_format=response_format, cancel_event=cancel_event,
                                fail_fast_on_429=bool(fallback))
    except Exception as e:
        if not fallback or fallback == model:
            raise
        reason = "hit its rate limit" if isinstance(e, RateLimitExceeded) else f"failed ({e})"
        log.warning("%s transcription with model %s %s, trying fallback %s",
                    cfg.stt_provider, model, reason, fallback)
        return _transcribe_once(cfg.stt_provider, audio_path, api_key, fallback, language=language,
                                response_format=response_format, cancel_event=cancel_event)


def transcribe(cfg: Config, audio_path: Path, language: str, cancel_event=None) -> str:
    """/audio/transcriptions (not /audio/translations) at a KNOWN language — keeps the source
    language. Plain text only; see transcribe_verbose when segment timing is needed too."""
    if cfg.use_local_whisper:
        return _run_local_whisper(cfg, audio_path, language, cancel_event=cancel_event)["text"]
    return _transcribe_with_fallback(cfg, audio_path, language, "json", cancel_event=cancel_event)


def transcribe_verbose(cfg: Config, audio_path: Path, language: Optional[str], cancel_event=None) -> dict:
    """Same call as transcribe(), but Whisper's full verbose_json response: {"text", "language",
    "segments": [{"start","end","text"}, ...]}. Same request, same price -- the segment timing
    is what makes the per-clip anchors (subtitles.clip_anchor_shift) possible, so every clip
    this module transcribes uses this shape and caches the segments (db.save_transcript_cache)
    for whoever checks the same video next. language=None -> Whisper detects it.

    use_local_whisper routes this through _run_local_whisper instead."""
    if cfg.use_local_whisper:
        return _run_local_whisper(cfg, audio_path, language, cancel_event=cancel_event)
    return _transcribe_with_fallback(cfg, audio_path, language, "verbose_json", cancel_event=cancel_event)


def detect_language_and_transcribe(cfg: Config, audio_path: Path, cancel_event=None):
    """No 'language' sent -> Whisper guesses itself. (language, text) -- kept for callers that
    only need the flat text; correctness_check itself uses transcribe_verbose directly."""
    result = transcribe_verbose(cfg, audio_path, None, cancel_event=cancel_event)
    return result.get("language"), (result.get("text") or "").strip()


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
    provider = cfg.stt_provider
    llm_model = cfg.openrouter_llm_model if provider == "openrouter" else cfg.groq_llm_model
    fallback_model = (cfg.openrouter_llm_model_fallback if provider == "openrouter"
                       else cfg.groq_llm_model_fallback) or None
    key = (provider, llm_model, text)
    hit = _TRANSLATION_MEMO.get(key)
    if hit is not None:
        return hit
    translated = translate_text(text, "English", provider=provider, api_key=cfg.active_stt_api_key,
                                llm_model=llm_model, llm_model_fallback=fallback_model, cancel_event=cancel_event)
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
    return {"transcript_excerpt": transcript[:160], "score": round(score, 3) if score is not None else None}


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
    rows = db.get_cached_transcripts_for_video(conn, video_path)
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


def full_transcript_cache_key(cfg: Config) -> tuple[str, str]:
    """(provider, model) the full-transcript cache is keyed on. One function so a writer and a
    reader can't disagree about the key -- a mismatch looks like an empty cache, and the caller
    silently goes and transcribes the whole file again."""
    if cfg.use_local_whisper:
        return "local", Path(cfg.local_whisper_model).name
    if cfg.stt_provider == "openrouter":
        return cfg.stt_provider, cfg.openrouter_stt_model
    return cfg.stt_provider, cfg.groq_model


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
    segments = cached["segments"]

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
    video_transcript_cache (keyed on video + slot index, not subtitle) before calling Whisper,
    and saved there after a fresh call. The audio doesn't change with the subtitle, so this
    benefits ANY later check of the same video: a remediation candidate tried right after
    another (see bazarr.verify_subtitle_candidate), a different language, or a normal Scan of
    a since-replaced subtitle. Omit (None) to always transcribe fresh, e.g. a one-off caller
    with no DB handle."""
    duration = get_duration_seconds(video_path)
    if not duration:
        return {"skipped": True, "reason": "could not read duration (ffprobe)"}

    if cfg.use_local_whisper:
        log.info("Whisper: local (whisper.cpp, %s)", Path(cfg.local_whisper_model).name)
    else:
        log.info("Whisper: %s (cloud)", cfg.stt_provider)

    audio_lang = detect_audio_language_ffprobe(video_path)
    if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
        # ffprobe's language tag alone is enough to decide this — skip sampling entirely.
        reason = f"speech is '{audio_lang}' (per the file's metadata), not '{cfg.require_audio_lang}' — skipped"
        return {"skipped": True, "reason": reason}

    # ONE sample count for both series and movies — a longer file isn't harder to verify,
    # it's still the same "does this dialogue match the audio" question. duration is split
    # into cfg.sample_count equal regions (spread evenly across the whole file), and in each
    # region the most dialogue-dense clip start (pick_dialogue_dense_time) is picked instead
    # of a blind timestamp — avoids landing a sample on a silent or action-heavy stretch.
    n = max(1, cfg.sample_count)
    regions = [(duration * i / n, duration * (i + 1) / n) for i in range(n)]
    transcript_lang = audio_lang or cfg.require_audio_lang

    # Speech timeline for VAD-guided placement (free when a prior check of this video --
    # any subtitle -- cached segments, else an optional Silero run, else None). None keeps
    # today's dialogue-density behavior bit-for-bit (see vad.pick_sample_time).
    timeline = vad.timeline_for_video(conn, video_path, cfg)
    built = []
    for idx, (region_start, region_end) in enumerate(regions):
        # The audio at a given point in this video doesn't depend on which subtitle is being
        # checked against it -- ANY earlier check of THIS video (a remediation candidate, a
        # different language, an earlier normal Scan, ...) may already have transcribed this
        # region's slot; reuse it and skip both pick_dialogue_dense_time and Whisper entirely.
        cached = (db.get_cached_transcript(conn, video_path, idx, within=(region_start, region_end))
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
                                         cfg.clip_seconds, base, cfg.vad_min_speech_seconds)
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
                                         clip_seconds=cfg.clip_seconds)

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
