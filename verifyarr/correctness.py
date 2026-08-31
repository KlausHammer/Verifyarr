"""Correctness check: Whisper transcription + optional translation, compared against the
subtitle's word content. See `correctness_check` for the main flow and scoring logic."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

from verifyarr import log
from verifyarr.settings import Config
from verifyarr.subtitles import pick_dialogue_dense_time, subs_text_in_window, tokenize

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


def extract_clip(video_path: Path, start_sec: float, duration_sec: int, out_path: Path) -> bool:
    cmd = ["ffmpeg", "-y", "-ss", str(max(0.0, start_sec)), "-t", str(duration_sec),
           "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
}


_last_call_at = 0.0  # module-level: only ever one job/check running at a time, see jobs.JobRunner


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
        if cancel_event is None:
            time.sleep(wait_s)
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise JobCancelled("cancelled while waiting for a rate limit")
            time.sleep(min(1.0, deadline - time.monotonic()))

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
                      fail_fast_on_429: bool = False):
    url = _STT_URLS[provider]
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": model, "response_format": response_format}
    if language:
        data["language"] = language
    last_err = None
    for attempt in range(retries + 1):
        try:
            log.debug("%s STT request: model=%s lang=%s clip=%s attempt=%d/%d",
                      provider, model, language or "auto", audio_path.name, attempt + 1, retries + 1)
            with open(audio_path, "rb") as f:
                files = {"file": (audio_path.name, f, "audio/wav")}
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
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or f"unknown {provider} error")


def _stt_model_and_fallback(cfg: Config) -> tuple[str, Optional[str]]:
    if cfg.stt_provider == "openrouter":
        return cfg.openrouter_stt_model, (cfg.openrouter_stt_model_fallback or None)
    return cfg.groq_model, (cfg.groq_model_fallback or None)


def transcribe(cfg: Config, audio_path: Path, language: str, cancel_event=None) -> str:
    """/audio/transcriptions (not /audio/translations) at a KNOWN language — keeps the source
    language. Tries the fallback model if the primary one fails — switching immediately
    (without waiting out the rate-limit period) if the failure was specifically a 429 and a
    fallback actually exists (see _post_ratelimited's fail_fast_on_429)."""
    model, fallback = _stt_model_and_fallback(cfg)
    api_key = cfg.active_stt_api_key
    try:
        return _transcribe_once(cfg.stt_provider, audio_path, api_key, model, language=language,
                                 response_format="json", cancel_event=cancel_event,
                                 fail_fast_on_429=bool(fallback))
    except Exception as e:
        if not fallback or fallback == model:
            raise
        reason = "hit its rate limit" if isinstance(e, RateLimitExceeded) else f"failed ({e})"
        log.warning("%s transcription with model %s %s, trying fallback %s",
                    cfg.stt_provider, model, reason, fallback)
        return _transcribe_once(cfg.stt_provider, audio_path, api_key, fallback, language=language,
                                 response_format="json", cancel_event=cancel_event)


def detect_language_and_transcribe(cfg: Config, audio_path: Path, cancel_event=None):
    """No 'language' sent -> Whisper guesses itself. Only used for the very first sample
    when neither the filename nor ffprobe could tell us what language is being spoken."""
    model, fallback = _stt_model_and_fallback(cfg)
    api_key = cfg.active_stt_api_key
    try:
        result = _transcribe_once(cfg.stt_provider, audio_path, api_key, model, language=None,
                                   response_format="verbose_json", cancel_event=cancel_event,
                                   fail_fast_on_429=bool(fallback))
    except Exception as e:
        if not fallback or fallback == model:
            raise
        reason = "hit its rate limit" if isinstance(e, RateLimitExceeded) else f"failed ({e})"
        log.warning("%s language-detection transcription with model %s %s, trying fallback %s",
                    cfg.stt_provider, model, reason, fallback)
        result = _transcribe_once(cfg.stt_provider, audio_path, api_key, fallback, language=None,
                                   response_format="verbose_json", cancel_event=cancel_event)
    return result.get("language"), (result.get("text") or "").strip()


def translate_to_english(cfg: Config, text: str, cancel_event=None) -> Optional[str]:
    if not text.strip():
        return ""
    provider = cfg.stt_provider
    url = _LLM_URLS[provider]
    api_key = cfg.active_stt_api_key
    llm_model = cfg.openrouter_llm_model if provider == "openrouter" else cfg.groq_llm_model
    fallback_model = (cfg.openrouter_llm_model_fallback if provider == "openrouter"
                       else cfg.groq_llm_model_fallback) or None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    models_to_try = [llm_model] + ([fallback_model] if fallback_model and fallback_model != llm_model else [])
    for i, model in enumerate(models_to_try):
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Translate the user's subtitle text to English. "
                                               "Output ONLY the translation, no notes or quotes."},
                {"role": "user", "content": text[:2000]},
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
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning("%s translation failed with model %s: %s", provider, model, e)
            continue
    return None


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


def correctness_check(video_path: Path, subs: "pysubs2.SSAFile", sub_lang: Optional[str],
                       cfg: Config, tmp_dir: Path, cancel_event=None) -> dict:
    """Standalone correctness check, isolated to one file — used by bazarr.py's
    verify_subtitle_candidate to vet a replacement subtitle before adopting it. The main
    sweep/scan flow (pipeline.process_pair) does NOT call this: it always goes through
    line_order.collect_samples()/finalize_line_order() instead, so a correctness check also
    collects (and caches, across runs) line-order candidate data at no extra cost — see
    line_order.py's module docstring."""
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
        start = pick_dialogue_dense_time(subs, region_start, region_end, cfg.clip_seconds)
        clip_path = tmp_dir / f"clip_{int(start)}.wav"
        if not extract_clip(video_path, start, cfg.clip_seconds, clip_path):
            built.append({"start": round(start, 1), "error": "audio extraction failed"})
            continue
        try:
            if audio_lang is None and idx == 0:
                # We don't know the audio language yet (neither ffprobe tags nor filename) —
                # let Whisper guess on the first sample, and reuse that for the rest of the file.
                detected, transcript = detect_language_and_transcribe(cfg, clip_path, cancel_event=cancel_event)
                audio_lang = detected
                transcript_lang = detected
            else:
                transcript = transcribe(cfg, clip_path, transcript_lang or "en", cancel_event=cancel_event)
        except Exception as e:
            built.append({"start": round(start, 1), "error": str(e)})
            continue
        finally:
            clip_path.unlink(missing_ok=True)

        if cfg.require_audio_lang and audio_lang and audio_lang != cfg.require_audio_lang:
            reason = f"speech is '{audio_lang}', not '{cfg.require_audio_lang}' — skipped"
            return {"skipped": True, "reason": reason}

        built.append({"start": round(start, 1), "transcript": transcript})

    samples = []
    for item in built:
        start = item["start"]
        if "error" in item:
            samples.append({"start": start, "error": item["error"]})
            continue
        transcript = item["transcript"]

        window_text = subs_text_in_window(
            subs, start, cfg.window_minutes * 60, cfg.clip_seconds + cfg.window_minutes * 60
        )
        compare = _compare_transcript_to_window(cfg, transcript, window_text, sub_lang, transcript_lang,
                                                  cancel_event=cancel_event)
        if "error" in compare:
            samples.append({"start": start, "error": compare["error"]})
            continue
        samples.append({"start": start, **compare})

    avg, flag = _aggregate_correctness(samples, cfg)
    return {"skipped": False, "avg_score": avg, "samples": samples, "flag": flag, "audio_lang": audio_lang}
