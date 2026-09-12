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
from verifyarr.procprio import wrap_low_priority
from verifyarr.settings import Config, VOCABULARY_HINT_MAX_CHARS
from verifyarr.subtitles import (
    pick_dialogue_dense_time, subs_text_in_window, tokenize,
    clip_anchor_shift, anchors_applicable, ANCHOR_SUSPECT_THRESHOLD_S,
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


def detect_audio_language_ffprobe(video_path: Path) -> Optional[str]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream_tags=language", "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return None
        tag = (streams[0].get("tags", {}) or {}).get("language", "").lower()
        if not tag or tag in ("und", "unk", "undefined"):
            return None
        return LANG3_TO_LANG2.get(tag, tag if LANG_CODE_RE.match(tag) else None)
    except Exception:
        return None


def detect_embedded_subtitle_langs(video_path: Path) -> set[str]:
    """Languages of every subtitle track baked into the video container itself, via ffprobe.
    Used so a video isn't flagged 'missing' a language just because there's no separate
    subtitle FILE for it -- Bazarr already considers an embedded track as satisfying that
    language and won't download a separate one either, and this tool has no way to
    sync/verify an embedded track (only external files), so there's nothing to do for it."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "s",
           "-show_entries", "stream_tags=language", "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return set()
    langs = set()
    for stream in data.get("streams", []):
        tag = (stream.get("tags", {}) or {}).get("language", "").lower()
        if not tag or tag in ("und", "unk", "undefined"):
            continue
        mapped = LANG3_TO_LANG2.get(tag, tag if LANG_CODE_RE.match(tag) else None)
        if mapped:
            langs.add(mapped)
    return langs


def get_duration_seconds(video_path: Path) -> Optional[float]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:
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
    """subprocess.run, except a cancel_event set mid-run kills the process and raises
    JobCancelled instead of waiting it out -- needed here (unlike extract_clip's plain
    subprocess.run) because a CPU-only local Whisper pass over even a short clip can take
    noticeably longer than a network round-trip, long enough that Cancel doing nothing until it
    finishes would be a real regression from the cloud path's cancel_event handling."""
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
    """whisper.cpp's own `-oj`/--output-json shape -> the same {"text","language","segments":
    [{"start","end","text"}]} shape transcribe_verbose's cloud path returns, so every caller
    (subtitles.clip_anchor_shift, correctness_check's scoring, line_order.py) works unchanged
    regardless of which path actually ran. offsets are milliseconds; segments/[].start,end here
    are seconds, matching Groq/OpenRouter's verbose_json convention."""
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
    """Surfaces whisper.cpp's own one-line backend banner (e.g. "... | Vulkan : ... | CPU :
    ... |" when a GPU device was found and loaded, vs. just "... | CPU : ... |" plus a preceding
    "ggml_vulkan: No devices found." line when it wasn't) in verifyarr's own logs. This is the
    only way to tell whether GPU passthrough (Settings -> Correctness -> "Use GPU", and the
    container actually having /dev/dri -- see docker-compose.yml/the TrueNAS docs) is actually
    being used, short of shelling into the container -- so it's worth a real log line rather
    than being silently discarded with the rest of a successful run's stderr. Logged once per
    process, not once per clip: whether a GPU engaged doesn't change clip to clip."""
    global _local_whisper_backend_logged
    if _local_whisper_backend_logged:
        return
    _local_whisper_backend_logged = True
    for line in stderr.splitlines():
        if line.startswith("system_info:") or "ggml_vulkan" in line:
            log.info("local Whisper backend: %s", line.strip())


def _run_local_whisper(cfg: Config, audio_path: Path, language: Optional[str],
                        cancel_event=None) -> dict:
    """Runs the app's own local whisper.cpp build (Settings -> Correctness -> "Use local
    Whisper") instead of a cloud STT call -- see Config.use_local_whisper's docstring for what
    this does and doesn't cover. No API key, no rate limiting, no fallback model (there's only
    the one local model configured) -- failures just raise, same contract the cloud path already
    has (a JobCancelled or RuntimeError per failed sample, handled by whoever calls transcribe*/
    collect_samples). GPU use (Vulkan) is whisper.cpp's own default whenever it was built with
    it and a usable device is found -- local_whisper_use_gpu=False passes -ng to force CPU only,
    e.g. for a host where the iGPU turned out not to help or isn't available."""
    binary, model = cfg.local_whisper_binary, cfg.local_whisper_model
    if not binary or not Path(binary).is_file():
        raise RuntimeError(f"local Whisper binary not found: {binary!r} (see Settings -> Correctness)")
    if not model or not Path(model).is_file():
        raise RuntimeError(f"local Whisper model not found: {model!r} (see Settings -> Correctness)")

    with tempfile.TemporaryDirectory(prefix="local-whisper-") as td:
        out_stem = Path(td) / "out"
        # Deliberately NOT passing -nt/--no-timestamps: despite the name, that doesn't just
        # silence console printing -- it feeds into whisper_full_params.no_timestamps and turns
        # off the model's own timestamp-token decoding, collapsing segmentation (needed for
        # per-line anchors) into far fewer/coarser segments than normal.
        cmd = [binary, "-m", model, "-f", str(audio_path), "-oj", "-of", str(out_stem),
               "-t", str(max(1, cfg.local_whisper_threads)), "-l", language or "auto"]
        if not cfg.local_whisper_use_gpu:
            cmd.append("-ng")
        log.debug("local Whisper: model=%s lang=%s clip=%s", Path(model).name, language or "auto",
                  audio_path.name)
        # Generous timeout: this runs on whatever CPU/iGPU the box has, which can be far slower
        # than a cloud endpoint for the same clip -- see clip_seconds' own docstring for the
        # longest a clip is ever expected to be (a few dozen seconds), so even a real-time-factor
        # well below 1x on weak hardware fits comfortably inside this.
        returncode, _stdout, stderr = _run_cancellable(wrap_low_priority(cmd), timeout=300,
                                                        cancel_event=cancel_event)
        if returncode != 0:
            raise RuntimeError(f"local Whisper failed (exit {returncode}): {stderr[-500:]}")
        _log_local_whisper_backend_info(stderr)
        out_path = out_stem.with_suffix(".json")
        if not out_path.exists():
            raise RuntimeError(f"local Whisper produced no output file: {stderr[-300:]}")
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"local Whisper produced unparseable output: {e}") from e
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

    use_local_whisper routes this (and transcribe() above) through _run_local_whisper instead —
    see Config.use_local_whisper's docstring for exactly what that does and doesn't replace."""
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


def significant_anchor_residuals(samples: list[dict],
                                 threshold: float = ANCHOR_SUSPECT_THRESHOLD_S) -> list[dict]:
    """Samples whose Whisper-anchor shift (subtitles.clip_anchor_shift — a content-VERIFIED
    point estimate of the real timing offset at that exact instant, not the bag-of-words window
    score) exceeds `threshold` seconds (default subtitles.ANCHOR_SUSPECT_THRESHOLD_S -- see
    there for why it is NOT sync.min_change_seconds). A confident anchor (one that survived the
    median/MAD agreement check) showing a real residual mismatch is independent evidence of a
    problem AT THAT SPECIFIC POINT even when the whole-file average/majority-vote already
    passed — see Config.anchor_check_enabled for the motivating case (a subtitle that's only
    right for part of the episode)."""
    return [s for s in samples if s.get("anchor") and abs(s["anchor"]["shift"]) > threshold]


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
        if use_anchors and r.get("segments"):
            anchor = clip_anchor_shift(r["segments"], start, subs, start - window_before, start + window_after)
        sample = {"start": start, "anchor": anchor}
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
            segments = cached.get("segments") or []
            if audio_lang is None:
                audio_lang = cached["audio_lang"]
                transcript_lang = audio_lang or cfg.require_audio_lang
        else:
            start = pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)
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
        if item.get("segments") and anchors_applicable(sub_lang, transcript_lang):
            anchor = clip_anchor_shift(item["segments"], item["clip_start"], subs,
                                       item["clip_start"] - window_before, item["clip_start"] + window_after)
        compare = _compare_transcript_to_window(cfg, transcript, window_text, sub_lang, transcript_lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": start, "error": compare["error"], "anchor": anchor})
            continue
        samples.append({"start": start, "anchor": anchor, **compare})

    avg, flag = _aggregate_correctness(samples, cfg)
    return {"skipped": False, "avg_score": avg, "samples": samples, "flag": flag, "audio_lang": audio_lang}
