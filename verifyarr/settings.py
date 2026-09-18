"""
Configuration. `Config.from_db(conn)` reads from the `settings` table in verifyarr.db
(edited via the webapp's Settings pages or `PUT /api/settings/{group}`) — the ONLY source
the CLI and the webapp use, so a setting changed in the UI applies immediately to both a
manual "run now" and Bazarr's post-processing hook, no restart needed.

All paths under /data (the database itself, backups, reports, quarantine) are deliberately
NOT configurable settings — only docker-compose.yml's `/data` volume decides them, per the
rule that compose should only hold real Docker requirements."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace as _dataclass_replace
from pathlib import Path
from typing import Optional

VIDEO_EXTS_DEFAULT = {".mkv", ".mp4", ".avi", ".m2ts", ".ts"}

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEFAULT_DB_PATH = DATA_DIR / "verifyarr.db"
DEFAULT_BACKUP_DIR = DATA_DIR / "backups"
DEFAULT_REPORT_DIR = DATA_DIR / "reports"
DEFAULT_QUARANTINE_DIR = DATA_DIR / "quarantine"

# Which local Whisper model ships as the default (Settings -> Correctness -> "Model file path")
# -- a real Docker requirement (which model the image can use without downloading), so it's a
# docker-compose env var, same as DATA_DIR above, not a settings-table value. Matches the
# Dockerfile's own WHISPER_MODEL build arg default -- change one, change the other to keep them
# in sync. tiny.en over the small.en family: chosen on WORST-FILE behaviour, not mean score --
# all 15 models sweep within 0.77-0.82 on the mean, but the bottom-3 mean per model separates them
# (tiny.en greedy 0.899, small.en-q5_1 0.854, small.en 0.735), and anchor yield is flat across all
# of them (74-77 confident windows). ~75MB and CPU-only, which drops the Vulkan dependency
# entirely. English-only, so see _run_local_whisper's language handling.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny.en")
_WHISPER_MODEL_FILENAME = f"ggml-{WHISPER_MODEL}.bin"
_BAKED_IN_WHISPER_MODEL_PATH = Path("/app/models") / _WHISPER_MODEL_FILENAME
# The Dockerfile only bakes in ONE model (whatever WHISPER_MODEL was at build time) -- if this
# env var asks for a DIFFERENT one, correctness._download_local_whisper_model fetches it into
# /data instead (persisted by docker-compose's existing /data volume, no separate volume needed).
DEFAULT_LOCAL_WHISPER_MODEL_PATH = (_BAKED_IN_WHISPER_MODEL_PATH if _BAKED_IN_WHISPER_MODEL_PATH.is_file()
                                    else DATA_DIR / "whisper-models" / _WHISPER_MODEL_FILENAME)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "ja", "on")


def _env_list(name: str, default: str) -> list[str]:
    val = os.environ.get(name, default)
    return [x.strip() for x in val.split(",") if x.strip()]


def normalize_url(url: str) -> str:
    """If the user enters e.g. just '192.168.1.32:30046' with no scheme, assume http:// —
    these apps rarely run behind https on a local network, so requiring a scheme was just
    friction. If https is actually needed, it still works fine since we only add something
    when no scheme is already present. Used for bazarr.url."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"http://{url}"
    return url


LANG_CODE_RE = re.compile(r"^[a-z]{2,3}$")

# generate.vocabulary_hint goes straight into Whisper's own "prompt" field, which is not an
# instruction channel -- it is decoder priming, and the model will happily continue whatever
# shape of text it is given. A short comma-separated list of names primes vocabulary; a labelled
# or sentence-shaped prompt ("Characters: Jeff, Britta...") primes PROSE, and was observed in
# real testing to make Whisper invent extra dialogue at the end of a clip, bleeding words from
# the prompt itself into the transcript. A warning in a tooltip does not prevent that, so the
# shape is enforced here instead: the value is capped in length and flattened to a single line.
VOCABULARY_HINT_MAX_CHARS = 200


def clean_vocabulary_hint(raw: Optional[str]) -> Optional[str]:
    """Flattens a vocabulary hint to one line and caps its length -- see
    VOCABULARY_HINT_MAX_CHARS. Applied both on save (so the stored value is already clean) and
    on read (so a value stored by an older build, or written straight into the settings table,
    can't slip past)."""
    if not raw:
        return None
    hint = " ".join(str(raw).split())
    return hint[:VOCABULARY_HINT_MAX_CHARS].strip() or None


def clean_lang_code(raw: Optional[str]) -> str:
    """A single ISO 639-1/639-2-shaped code, lowercased -- or "" if it isn't one. Guards the
    settings that end up in a FILENAME (see fileops.write_new_subtitle) or in an API request's
    `language` field, where free text is at best ignored and at worst produces a subtitle
    discovery can never find again."""
    value = (raw or "").strip().lower()
    return value if LANG_CODE_RE.match(value) else ""


def _parse_path_map(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for chunk in _env_list("PATH_MAP", raw):
        if "=" not in chunk:
            continue
        local, bazarr = chunk.split("=", 1)
        pairs.append((local.rstrip("/"), bazarr.rstrip("/")))
    return pairs


def _is_under(path: Path, root: Path) -> bool:
    """Whether path lives anywhere under root. One place for the try/relative_to idiom
    so kind_for and media_root_for can't disagree on what "under" means."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass
class Config:
    movies_folder: Optional[Path]
    series_folder: Optional[Path]
    subtitle_langs: list[str]
    video_exts: set[str]
    # Whether a manual Scan fixes subtitle timing (the alass step) at all — its own switch. The
    # scheduled sweep and the Bazarr wanted-subtitles poll share a SEPARATE switch instead, see
    # auto_scan_sync_enabled below; jobs._effective_cfg swaps this field out entirely (not an AND)
    # for those two triggers. See Settings -> Automation's "What runs" table.
    sync_enabled: bool
    split_penalty: int
    state_db: Path
    backup_dir: Path
    report_dir: Path
    dry_run: bool
    min_change_seconds: float

    enable_correctness_check: bool
    stt_provider: str  # groq | openrouter — used for BOTH transcription and translation
    groq_api_key: Optional[str]
    groq_model: str
    groq_model_fallback: Optional[str]
    groq_llm_model: str
    groq_llm_model_fallback: Optional[str]
    openrouter_api_key: Optional[str]
    openrouter_stt_model: str
    openrouter_stt_model_fallback: Optional[str]
    openrouter_llm_model: str
    openrouter_llm_model_fallback: Optional[str]
    # Runs actual TRANSCRIPTION (transcribe/transcribe_verbose — the per-clip Whisper calls that
    # dominate correctness-check's API usage) through a local whisper.cpp binary instead of
    # stt_provider's cloud endpoint — no network call, no API key, no rate limit, no per-request
    # cost. Deliberately narrow: translation (translate_to_english) is a plain-text
    # chat-completion call, not speech recognition, so it keeps using stt_provider's cloud key
    # exactly as before regardless of this flag — a local setup that also wants translated
    # subtitles either keeps a (much cheaper, rarely-hit) cloud key for just that, or only checks
    # same-language subtitles. See has_stt_configured, and correctness._run_local_whisper for the
    # actual subprocess call. Aimed at Intel iGPU boxes (e.g. N100) via whisper.cpp's Vulkan
    # backend baked into the Docker image — see Dockerfile — but works anywhere the binary+model
    # exist, GPU or not.
    use_local_whisper: bool
    local_whisper_binary: str
    local_whisper_model: str
    local_whisper_use_gpu: bool
    local_whisper_threads: int
    # VAD timeline traffic control (see vad.py). Binary+model are local-only and optional:
    # empty means "no binary timeline", and placement falls back to cached transcript
    # segments, then to subtitle dialogue density. Nothing here is language-specific.
    vad_binary: str
    vad_model: str
    vad_min_speech_seconds: float
    # ONE count for both series and movies -- a longer file isn't harder to verify, it's still
    # the same "does this dialogue match the audio" question, so there's no reason to sample it
    # more. 5 rather than 3 because of the TIMING screen, not the content check: content
    # separates perfectly at 3 clips, but 3 clips only produce ~2.3 confident anchors and catch
    # 7 of 10 mis-timed files, where 5 clips produce ~3.9 and catch 9 of 10 (see
    # pipeline._screen_says_needs_full).
    sample_count: int
    clip_seconds: int
    window_minutes: float
    overlap_threshold: float
    require_audio_lang: Optional[str]
    # sampled (default) = a handful of short clips, as above. full = one whole-file Whisper
    # transcription (cached per video), used to sync-verify/correctness-check/line-order-check
    # EVERY line instead of a few samples -- much stronger, but far more Whisper work per file
    # (local Whisper: mostly time; cloud: real API cost/quota). See generate.full_transcript_for_check.
    whisper_mode: str
    # A big spread between alass's own per-block shifts (see pipeline.sync_pair's shift_blocks)
    # CAN mean a real structural cut, but it's also exactly what a wrong-but-similarly-paced
    # subtitle looks like (alass only fits speech TIMING, not content). Escalates an otherwise-
    # "ok" majority-vote correctness verdict to SUSPECT, but ONLY when at least one individual
    # Whisper sample ALSO disagreed on its own (see correctness_and_finish) -- spread alone isn't
    # enough, since a real cut can legitimately produce a big spread with every sample still
    # matching fine.
    block_spread_suspect_threshold_s: float
    # Whisper-anchor ESCALATION (see subtitles.clip_anchor_shift): a Whisper segment that
    # clearly matches one specific subtitle line is a content-VERIFIED point estimate of the
    # real timing offset right there, independent of the majority-vote average. With this on, a
    # confident anchor showing a residual mismatch above subtitles.ANCHOR_SUSPECT_THRESHOLD_S
    # (an anchor-noise-aware threshold, NOT min_change_seconds) escalates an otherwise-"ok" file
    # to SUSPECT -- catches a subtitle that's only right for PART of the episode (see
    # block_spread_suspect_threshold_s' own docstring for the motivating case). ON by default
    # since being validated against 52 real episodes: it found 9 genuinely misaligned files alass
    # had reported "already in sync", with 0 false escalations once correctness.
    # ANCHOR_SUSPECT_MIN_SAMPLES required 3 agreeing residuals instead of accepting a single one.
    #
    # What this flag does NOT gate: anchors are always COMPUTED (CPU only, cached with the
    # sample), and pipeline._resolve_ambiguous_sync always uses them to CHOOSE between alass's
    # own sync candidates when alass produced a multi-block fit -- that choice only ever picks
    # among alass's outputs and the original, it never flags a file, and without anchors it
    # simply trusts alass's single-offset fit. Anchors only exist at all for a subtitle in the
    # spoken language (subtitles.anchors_applicable) -- a Danish subtitle on English audio has
    # none, so neither this escalation nor the candidate choice has timing evidence for it.
    anchor_check_enabled: bool
    # With this on, a file the anchors condemn is CORRECTED from those same anchors rather than
    # only flagged -- per-region, so a file that needs different offsets in different stretches is
    # handled too. The correction is re-measured against the audio before anything is written, and
    # a file whose anchor evidence isn't consistent enough to plan from (a continuous drift, say)
    # falls back to the ordinary SUSPECT path untouched. Needs anchor_check_enabled.
    anchor_resync_enabled: bool
    # Sampled mode screens with a few clips; when those clips disagree about the offset (or one
    # shows a real residual), a handful of probes cannot say WHERE the file changes -- measured,
    # escalating with more probes plans the correction right on only 4 of 9 real files, while
    # escalating to a full transcript gets 8 of 9. So a flagged file is re-checked against the
    # whole transcript instead. Costs a full transcription, but only for files that screened bad.
    escalate_sampled_to_full: bool
    escalate_min_bad_samples: int
    anchor_suspect_min_samples: int
    escalate_only_multi_block: bool

    # Line-order check (see line_order.py). Off by default, opt-in.
    line_order_enabled: bool
    line_order_audio_confirm: bool
    # "Is this subtitle bad enough to replace" threshold (check_subtitle, line_order.py) —
    # applied independently to BOTH Whisper's and the LLM's confirmed-swap rate on whatever was
    # actually sampled (bounded by sync.sample_count — line-order no longer has a separate sample
    # budget). Both signals must clear it before a redownload.
    line_order_swap_threshold_pct: float
    line_order_swap_threshold_min: int

    quarantine_dir: Path
    bazarr_url: Optional[str]
    bazarr_api_key: Optional[str]
    path_map: list[tuple[str, str]]
    remediate_max_attempts: int
    remediate_min_score: float

    log_level: str = "INFO"
    sweep_cron: str = "0 4 * * 0"
    run_on_start: bool = False
    poll_new_media_enabled: bool = True
    poll_new_media_interval_minutes: int = 10
    # Separate from poll_new_media_* above (that one is Bazarr's wanted-lists poll, not a
    # filesystem check) -- a periodic, discovery-only refresh of the Library page's cache (no
    # sync/correctness/API calls, just the same directory walk a sweep already does at the start
    # of _run_sweep) so newly added media shows up under Movies/Series without waiting for a
    # sweep or a manual Scan/Rescan click. See library_poll.py.
    poll_library_enabled: bool = True
    poll_library_interval_minutes: int = 720  # 12 hours

    # ON by default — see fileops.backup_subtitle. Gates EVERY backup_subtitle call (both the
    # ordinary sync-fix and the line-order auto-fix), not a separate switch per feature — see
    # pipeline.py. On rather than off because this app rewrites subtitles in place in three
    # different places, and the line-order auto-fix runs at ~98% precision -- measured, so ~2% of
    # its swaps are wrong, and without this there is nothing to undo them with. A failed backup
    # never blocks the fix itself (pipeline._backup_best_effort).
    backup_originals: bool = True

    # A manual Scan (and CLI calls) always use sync_enabled/enable_correctness_check/
    # line_order_enabled above as-is — its own, independent switch. The scheduled sweep AND the
    # Bazarr wanted-subtitles poll SHARE this second set instead (jobs._effective_cfg replaces,
    # not narrows, the switches above with these for both of those triggers) — one "automatic"
    # setting rather than three separate on/off questions to keep in sync. See Settings ->
    # Automation's "What runs" table.
    auto_scan_sync_enabled: bool = True
    auto_scan_correctness_enabled: bool = True
    auto_scan_line_order_enabled: bool = False

    # What to do with a file the CORRECTNESS check flags SUSPECT (off | quarantine | blacklist |
    # remediate). There is deliberately no line-order equivalent: a confirmed swap is a safe
    # mechanical per-line fix (measured precision 0.98 against an independent reference), so
    # "the lines are out of order" is never on its own a reason to throw the file away and fetch
    # another release. A real content problem is caught by the correctness score instead.
    correctness_auto_action: str = "off"

    # Same "manual Scan/CLI has its own switch, scheduled sweep + Bazarr poll share a second
    # one" split as sync_enabled/auto_scan_sync_enabled above -- see jobs._effective_cfg.
    generate_enabled: bool = False
    auto_scan_generate_enabled: bool = False
    # groq | openrouter | cloudflare -- see generate.py's per-provider STT adapters. Deliberately
    # its OWN api keys/models per provider (not shared with correctness.* above) so generation's
    # heavier full-track quota usage never competes with -- or is silently capped by -- whatever
    # a user has configured for the cheap sampled correctness check.
    generate_stt_provider: str = "groq"
    generate_groq_api_key: Optional[str] = None
    generate_groq_stt_model: str = "whisper-large-v3"
    generate_groq_stt_model_fallback: Optional[str] = "whisper-large-v3-turbo"
    generate_openrouter_api_key: Optional[str] = None
    generate_openrouter_stt_model: str = "openai/whisper-large-v3"
    generate_openrouter_stt_model_fallback: Optional[str] = None
    generate_cloudflare_account_id: Optional[str] = None
    generate_cloudflare_api_token: Optional[str] = None
    generate_cloudflare_stt_model: str = "@cf/openai/whisper-large-v3-turbo"
    # How many seconds of (compressed) audio to send per STT request -- per provider, since each
    # has a different practical size/duration limit (Cloudflare's in particular is undocumented
    # and empirically much smaller than Groq/OpenRouter's -- see generate.py).
    generate_chunk_seconds_groq: int = 600
    generate_chunk_seconds_openrouter: int = 600
    generate_chunk_seconds_cloudflare: int = 60
    generate_audio_bitrate_kbps: int = 64
    # Fallback spoken-language assumption when a provider can't tell us (Cloudflare's Whisper
    # models don't reliably report a detected language) and ffprobe's own audio-stream tag is
    # also missing/unusable. Empty = give up and skip that video rather than guess.
    generate_assume_spoken_lang: Optional[str] = None
    # Free-text vocabulary hint (Whisper's own "prompt" field, Groq/OpenRouter only -- Cloudflare's
    # endpoint has no equivalent) -- e.g. a show's character names, to reduce mishearing of proper
    # nouns/invented words a generic model has no other way to know about. Empty = no hint sent.
    # Keep it a short, plain comma-separated list (e.g. "Jeff, Britta, Abed, Chang") -- a labeled
    # or sentence-shaped prompt ("Characters: ...") was observed to make Whisper hallucinate
    # extra, made-up dialogue near the end of a chunk that bleeds in words from the prompt itself.
    generate_vocabulary_hint: Optional[str] = None
    # groq | openrouter | gemini -- the LLM used to translate a generated subtitle into any
    # wanted language that isn't the spoken one (see generate.translate_segments).
    generate_llm_provider: str = "groq"
    generate_groq_llm_model: str = "openai/gpt-oss-20b"
    generate_groq_llm_model_fallback: Optional[str] = "allam-2-7b"
    generate_openrouter_llm_model: str = "openai/gpt-4o-mini"
    generate_openrouter_llm_model_fallback: Optional[str] = None
    generate_gemini_api_key: Optional[str] = None
    generate_gemini_llm_model: str = "gemini-2.0-flash"
    generate_gemini_llm_model_fallback: Optional[str] = None
    # Subtitle lines per translation LLM call (a numbered list) -- keeps each call small enough
    # to stay well clear of a free-tier's per-request token limit while still being far cheaper
    # than one call per line. See generate.translate_segments's per-batch validation.
    generate_translate_batch_size: int = 20
    # Caps how many DISTINCT VIDEOS get a subtitle generated per rolling 24 HOURS -- transcription
    # cost is per-video, so this is the actual quota-protecting knob (translating a video into
    # several extra wanted languages doesn't count again). Default is deliberately small: a free
    # API tier's daily quota is easy to exhaust on a handful of feature-length videos.
    # Deliberately a DAILY cap rather than a per-run one: the Bazarr wanted-subtitles poll starts
    # a fresh sweep every time any wanted item resolves (see bazarr_poll.py), so a per-run cap is
    # multiplied by however many times that fired today -- which is not a quota guard at all.
    # Counted from db.generate_attempts, so it survives restarts and spans every trigger.
    generate_max_videos_per_day: int = 3

    @classmethod
    def from_db(cls, conn) -> "Config":
        from verifyarr import db
        vals = get_all_settings(conn)
        return cls(
            movies_folder=Path(vals["general.movies_folder"]) if vals["general.movies_folder"] else None,
            series_folder=Path(vals["general.series_folder"]) if vals["general.series_folder"] else None,
            subtitle_langs=[l.lower() for l in vals["general.subtitle_langs"]],
            # Not a setting anymore — always all supported file types (see VIDEO_EXTS_DEFAULT).
            # Limiting it added complexity without real benefit; scope is instead controlled
            # via the Movies/Series folders.
            video_exts=set(VIDEO_EXTS_DEFAULT),
            sync_enabled=vals["sync.enabled"],
            # alass is baked into the Docker image (see Dockerfile) — nothing to configure,
            # `resolve_alass_bin()` (sync_engine.py) finds it the same way every time.
            split_penalty=vals["sync.split_penalty"],
            state_db=DEFAULT_DB_PATH,
            backup_dir=DEFAULT_BACKUP_DIR,
            report_dir=DEFAULT_REPORT_DIR,
            dry_run=vals["automation.dry_run"],
            min_change_seconds=vals["sync.min_change_seconds"],
            enable_correctness_check=vals["correctness.enabled"],
            stt_provider=vals["correctness.stt_provider"],
            groq_api_key=vals["correctness.groq_api_key"] or None,
            groq_model=vals["correctness.groq_model"],
            groq_model_fallback=vals["correctness.groq_model_fallback"] or None,
            groq_llm_model=vals["correctness.groq_llm_model"],
            groq_llm_model_fallback=vals["correctness.groq_llm_model_fallback"] or None,
            openrouter_api_key=vals["correctness.openrouter_api_key"] or None,
            openrouter_stt_model=vals["correctness.openrouter_stt_model"],
            openrouter_stt_model_fallback=vals["correctness.openrouter_stt_model_fallback"] or None,
            openrouter_llm_model=vals["correctness.openrouter_llm_model"],
            openrouter_llm_model_fallback=vals["correctness.openrouter_llm_model_fallback"] or None,
            use_local_whisper=vals["correctness.use_local_whisper"],
            local_whisper_binary=vals["correctness.local_whisper_binary"],
            local_whisper_model=vals["correctness.local_whisper_model"],
            local_whisper_use_gpu=vals["correctness.local_whisper_use_gpu"],
            local_whisper_threads=vals["correctness.local_whisper_threads"],
            vad_binary=vals["sync.vad_binary"],
            vad_model=vals["sync.vad_model"],
            vad_min_speech_seconds=vals["sync.vad_min_speech_seconds"],
            correctness_auto_action=vals["correctness.auto_action"],
            sample_count=vals["sync.sample_count"],
            clip_seconds=vals["sync.clip_seconds"],
            window_minutes=vals["sync.window_minutes"],
            overlap_threshold=vals["sync.overlap_threshold"],
            block_spread_suspect_threshold_s=vals["sync.block_spread_suspect_threshold_s"],
            anchor_check_enabled=vals["sync.anchor_check_enabled"],
            anchor_resync_enabled=vals["sync.anchor_resync_enabled"],
            escalate_sampled_to_full=vals["sync.escalate_sampled_to_full"],
            escalate_min_bad_samples=vals["sync.escalate_min_bad_samples"],
            anchor_suspect_min_samples=vals["sync.anchor_suspect_min_samples"],
            escalate_only_multi_block=vals["sync.escalate_only_multi_block"],
            require_audio_lang=vals["correctness.require_audio_lang"] or None,
            whisper_mode=vals["sync.whisper_mode"],
            line_order_enabled=vals["sync.line_order_enabled"],
            line_order_audio_confirm=vals["sync.line_order_audio_confirm"],
            line_order_swap_threshold_pct=vals["sync.line_order_swap_threshold_pct"],
            line_order_swap_threshold_min=vals["sync.line_order_swap_threshold_min"],
            quarantine_dir=DEFAULT_QUARANTINE_DIR,
            bazarr_url=normalize_url(vals["bazarr.url"]) or None,
            bazarr_api_key=vals["bazarr.api_key"] or None,
            path_map=[tuple(p) for p in vals["bazarr.path_map"]],
            remediate_max_attempts=vals["automation.remediate_max_attempts"],
            remediate_min_score=vals["automation.remediate_min_score"],
            log_level=vals["log.level"],
            sweep_cron=vals["scheduling.cron"],
            run_on_start=vals["scheduling.run_on_start"],
            poll_new_media_enabled=vals["scheduling.poll_new_media_enabled"],
            poll_new_media_interval_minutes=vals["scheduling.poll_new_media_interval_minutes"],
            poll_library_enabled=vals["scheduling.poll_library_enabled"],
            poll_library_interval_minutes=vals["scheduling.poll_library_interval_minutes"],
            auto_scan_sync_enabled=vals["general.auto_scan_sync_enabled"],
            auto_scan_correctness_enabled=vals["general.auto_scan_correctness_enabled"],
            auto_scan_line_order_enabled=vals["general.auto_scan_line_order_enabled"],
            backup_originals=vals["general.backup_originals"],
            generate_enabled=vals["generate.enabled"],
            auto_scan_generate_enabled=vals["general.auto_scan_generate_enabled"],
            generate_stt_provider=vals["generate.stt_provider"],
            generate_groq_api_key=vals["generate.groq_api_key"] or None,
            generate_groq_stt_model=vals["generate.groq_stt_model"],
            generate_groq_stt_model_fallback=vals["generate.groq_stt_model_fallback"] or None,
            generate_openrouter_api_key=vals["generate.openrouter_api_key"] or None,
            generate_openrouter_stt_model=vals["generate.openrouter_stt_model"],
            generate_openrouter_stt_model_fallback=vals["generate.openrouter_stt_model_fallback"] or None,
            generate_cloudflare_account_id=vals["generate.cloudflare_account_id"] or None,
            generate_cloudflare_api_token=vals["generate.cloudflare_api_token"] or None,
            generate_cloudflare_stt_model=vals["generate.cloudflare_stt_model"],
            generate_chunk_seconds_groq=vals["generate.chunk_seconds_groq"],
            generate_chunk_seconds_openrouter=vals["generate.chunk_seconds_openrouter"],
            generate_chunk_seconds_cloudflare=vals["generate.chunk_seconds_cloudflare"],
            generate_audio_bitrate_kbps=vals["generate.audio_bitrate_kbps"],
            generate_assume_spoken_lang=clean_lang_code(vals["generate.assume_spoken_lang"]) or None,
            generate_vocabulary_hint=clean_vocabulary_hint(vals["generate.vocabulary_hint"]),
            generate_llm_provider=vals["generate.llm_provider"],
            generate_groq_llm_model=vals["generate.groq_llm_model"],
            generate_groq_llm_model_fallback=vals["generate.groq_llm_model_fallback"] or None,
            generate_openrouter_llm_model=vals["generate.openrouter_llm_model"],
            generate_openrouter_llm_model_fallback=vals["generate.openrouter_llm_model_fallback"] or None,
            generate_gemini_api_key=vals["generate.gemini_api_key"] or None,
            generate_gemini_llm_model=vals["generate.gemini_llm_model"],
            generate_gemini_llm_model_fallback=vals["generate.gemini_llm_model_fallback"] or None,
            generate_translate_batch_size=vals["generate.translate_batch_size"],
            generate_max_videos_per_day=vals["generate.max_videos_per_day"],
        )

    @property
    def active_stt_api_key(self) -> Optional[str]:
        """The API key for the currently selected transcription/translation provider
        (correctness.stt_provider — groq or openrouter). One place to ask instead of every
        caller needing to know both fields and switch on the provider name."""
        return self.openrouter_api_key if self.stt_provider == "openrouter" else self.groq_api_key

    @property
    def has_stt_configured(self) -> bool:
        """Whether correctness.py has SOMETHING it can transcribe audio with — either a cloud
        API key, or use_local_whisper (which needs no key at all). The gate every "can a
        correctness check even run" check should use INSTEAD OF active_stt_api_key directly —
        that one alone would wrongly say "not configured" for a local-only setup with no cloud
        key. Doesn't verify local_whisper_binary/local_whisper_model actually point at real
        files — that failure surfaces per-sample instead (same as a cloud key that turns out to
        be invalid only failing on first use), not as a static config gate.

        The binary IS checked, because local Whisper is now the default: without it a box that
        never installed whisper-cli reports "STT configured", fails on every clip, and every file
        it touches comes out SUSPECT — an accusation against the subtitle for a missing
        dependency. Not verifying anything has to mean "skipped", never "bad". The model file is
        deliberately not checked: it downloads itself on first use."""
        if self.use_local_whisper:
            return bool(self.local_whisper_binary) and Path(self.local_whisper_binary).is_file()
        return bool(self.active_stt_api_key)

    @property
    def active_generate_stt_api_key(self) -> Optional[str]:
        """Same idea as active_stt_api_key, but for generate_stt_provider's own (separate) set
        of keys -- Cloudflare's "key" is really its API token, used the same way here."""
        if self.generate_stt_provider == "cloudflare":
            return self.generate_cloudflare_api_token
        if self.generate_stt_provider == "openrouter":
            return self.generate_openrouter_api_key
        return self.generate_groq_api_key

    @property
    def active_generate_llm_api_key(self) -> Optional[str]:
        if self.generate_llm_provider == "gemini":
            return self.generate_gemini_api_key
        if self.generate_llm_provider == "openrouter":
            return self.generate_openrouter_api_key
        return self.generate_groq_api_key

    def generate_chunk_seconds_for(self, provider: str) -> int:
        """Per-provider STT chunk length in seconds (see generate.py) -- each provider has a
        different practical per-request audio size/duration limit, so this is deliberately not
        one global value."""
        return {
            "groq": self.generate_chunk_seconds_groq,
            "openrouter": self.generate_chunk_seconds_openrouter,
            "cloudflare": self.generate_chunk_seconds_cloudflare,
        }.get(provider, self.generate_chunk_seconds_groq)

    @property
    def media_roots(self) -> list[Path]:
        """The actually configured root folder(s) — Movies and/or Series, in that order. Kept
        as a property (instead of rewriting all discovery code for two separate paths) so
        discovery.py's `for root in cfg.media_roots: os.walk(root)` pattern keeps working
        whether one, both, or (briefly, before initial setup) neither is set."""
        return [p for p in (self.movies_folder, self.series_folder) if p]

    def kind_for(self, path: Path) -> str:
        """'movie' | 'series' | 'unknown' — authoritative via which of the two configured
        folders the file lives under; falls back to an SxxEyy-pattern guess (same as
        discovery.infer_title_and_episode) for files that aren't under either yet. Used for
        the Movies/Series tabs in the webapp's library view (see
        discovery.build_library_video_rows)."""
        if self.series_folder and _is_under(path, self.series_folder):
            return "series"
        if self.movies_folder and _is_under(path, self.movies_folder):
            return "movie"
        from verifyarr.discovery import infer_title_and_episode
        season_episode, _title = infer_title_and_episode(path)
        return "series" if season_episode else "movie"

    def media_root_for(self, path: Path) -> Path:
        for root in self.media_roots:
            if _is_under(path, root):
                return root
        return path.parent

    def with_dry_run(self, dry_run: bool) -> "Config":
        return self if self.dry_run == dry_run else _dataclass_replace(self, dry_run=dry_run)


# --- typed settings layer on top of db.py's flat key/value table --------------------------------
# (group, type, default). type controls (de)serialization: str/int/float/bool/list/list_pairs.
# The group name is also the URL segment under /api/settings/{group} and /settings/{group}.
SETTING_DEFS: dict = {
    "general.movies_folder":   ("general", "str", ""),
    "general.series_folder":   ("general", "str", ""),
    "general.subtitle_langs":  ("general", "list", ["en"]),
    # On by default — see fileops.backup_subtitle / pipeline.py. Gates every backup, not just one
    # feature's.
    "general.backup_originals": ("general", "bool", True),
    # What the scheduled sweep AND the Bazarr wanted-subtitles poll do (shared — NOT a manual
    # Scan, which uses sync.enabled/correctness.enabled/sync.line_order_enabled as its own,
    # separate switch). See jobs._effective_cfg / pipeline.py.
    "general.auto_scan_sync_enabled":        ("general", "bool", True),
    "general.auto_scan_correctness_enabled": ("general", "bool", True),
    "general.auto_scan_line_order_enabled":  ("general", "bool", False),
    "general.auto_scan_generate_enabled":    ("general", "bool", False),

    # sync.alass_bin removed — alass is baked into the Docker image, nothing to pick.
    "sync.enabled":            ("sync", "bool", True),
    "sync.split_penalty":      ("sync", "int", 7),
    # 0.5, not 0.25: the residual we can MEASURE on real files lands on a 0.25s grid (12
    # reference episodes, 144 bins -- nothing at all between 0 and 0.25), so 0.25 is one grid
    # step, i.e. inside the noise. 7.6% of real bins sit at 0.25-0.5s and are indistinguishable
    # from in-sync. A +0.4s "fix" also eats more than half of the subtitle's natural ~0.7s lead.
    "sync.min_change_seconds": ("sync", "float", 0.5),
    # Whisper sampling parameters — the same knobs control both how well sync finds the
    # shift and how well the correctness check compares, hence grouped with sync tuning.
    # ONE count for both series and movies — a longer file isn't harder to verify, no reason
    # to sample it more.
    # 16 short clips, not 5 long ones. Coverage dominates clip length, and the measure that
    # settles it is how often a broken file reaches the user with NEITHER a fix NOR a warning --
    # a silent pass is the only failure that actually hurts. Over 268 rows per scenario on all 15
    # models, sampled:
    #     5x60s  716 audio s/file   silent: piecewise 28%  drift 33%  drift_swap 33%
    #     8x20s   52               silent: piecewise 14%  drift 22%  drift_swap 16%
    #    12x20s   66               silent: piecewise 15%  drift  2%  drift_swap  3%
    #    16x15s   75               silent: piecewise 10%  drift  0%  drift_swap  0%
    # 16x15 costs 4 false SUSPECT per 298 clean files where 8x20 costs none, but a false SUSPECT
    # only writes a line in the report (correctness_auto_action defaults to off and nothing is
    # rewritten), while a silent pass is a subtitle the viewer has to notice themselves. 8x20
    # also rewrote 2 healthy files in full mode; 16x15 rewrites none in either mode.
    "sync.sample_count":        ("sync", "int", 16),
    "sync.clip_seconds":        ("sync", "int", 15),
    "sync.window_minutes":      ("sync", "float", 0.5),
    "sync.overlap_threshold":   ("sync", "float", 0.25),
    # sampled | full -- see Config.whisper_mode.
    "sync.whisper_mode":        ("sync", "str", "sampled"),
    # VAD timeline for sample placement (see vad.py). Empty binary = disabled; the model
    # is deliberately NOT auto-downloaded (no verified stable URL), mount or place it at
    # vad_model. Language-independent by construction: speech activity has no language.
    "sync.vad_binary":          ("sync", "str", ""),
    "sync.vad_model":           ("sync", "str", "/app/models/ggml-silero-v5.1.2.bin"),
    "sync.vad_min_speech_seconds": ("sync", "float", 2.0),
    # See Config.block_spread_suspect_threshold_s -- 20s chosen as "clearly more than ordinary
    # sync jitter" while still well below what a real recap/extended-cut spread usually looks
    # like (typically 60s+), and the required agreeing Whisper sample is the actual gate that
    # keeps a legitimate structural cut from being wrongly escalated.
    "sync.block_spread_suspect_threshold_s": ("sync", "float", 20.0),
    "sync.anchor_check_enabled": ("sync", "bool", True),
    "sync.anchor_resync_enabled": ("sync", "bool", True),
    # Off: transcribing the whole track costs ~10x a sampled run (517 vs 52 audio seconds per
    # file, measured) and buys almost nothing. What it actually did was make the SUSPECT rule
    # easier to satisfy -- 3 bad clips out of ~21 full-mode windows instead of out of 5 -- so
    # lowering anchor_suspect_min_samples to 2 gets the same detection for free. Measured over
    # 700 sampled rows per policy: drift 69->62%, drift_swap 68->71%, piecewise 17->24%, zero
    # false SUSPECT on clean either way, recovery 0.631 -> 0.629. Still a setting, because a
    # library with very long episodes may want the denser evidence.
    "sync.escalate_sampled_to_full": ("sync", "bool", False),
    # How many clips must show a real residual before the whole track is transcribed.
    # 2, not 1: measured over 700 sampled rows per policy, going from 1 to 2 cuts fresh Whisper
    # audio 30% (739 -> 517 s/file) for -0.001 recovery and -1pp detection. At 2 the
    # single-residual rule stops firing entirely -- every remaining escalation is spread-driven,
    # i.e. the block files _try_anchor_resync can actually fix. Turning it off altogether saves
    # 93% but drops drift detection from 70% to 45%, which is too much: escalation doesn't repair
    # drift, but it is what NOTICES it.
    "sync.escalate_min_bad_samples": ("sync", "int", 2),
    # How many clips must disagree before the FILE is called SUSPECT. An absolute count is a
    # much harder bar on 5 sampled clips (60%) than on a full transcript's ~21 windows (14%),
    # which is most of what escalating actually bought. 2, not 3: on 14 real healthy episodes not
    # one has even a SINGLE clip over ANCHOR_SUSPECT_THRESHOLD_S (worst is 1.52s), and 600 clean
    # matrix rows give zero false SUSPECT at 2 -- so the second clip is free evidence.
    "sync.anchor_suspect_min_samples": ("sync", "int", 2),
    "sync.escalate_only_multi_block": ("sync", "bool", True),
    # Off by default, opt-in — see line_order.py.
    "sync.line_order_enabled":       ("sync", "bool", False),
    "sync.line_order_audio_confirm": ("sync", "bool", False),
    "sync.line_order_swap_threshold_pct": ("sync", "float", 0.30),
    "sync.line_order_swap_threshold_min": ("sync", "int", 3),

    "correctness.enabled":                  ("correctness", "bool", True),
    "correctness.stt_provider":             ("correctness", "str", "groq"),  # groq | openrouter
    "correctness.groq_api_key":             ("correctness", "str", "", ),
    "correctness.groq_model":               ("correctness", "str", "whisper-large-v3"),
    # Falls back to the turbo model only when the primary hits its own rate limit (fail-fast,
    # doesn't wait it out) — see _post_ratelimited's fail_fast_on_429 in correctness.py.
    "correctness.groq_model_fallback":      ("correctness", "str", "whisper-large-v3-turbo"),
    "correctness.groq_llm_model":           ("correctness", "str", "openai/gpt-oss-20b"),
    "correctness.groq_llm_model_fallback":  ("correctness", "str", "allam-2-7b"),
    "correctness.openrouter_api_key":            ("correctness", "str", ""),
    "correctness.openrouter_stt_model":          ("correctness", "str", "openai/whisper-large-v3"),
    "correctness.openrouter_stt_model_fallback": ("correctness", "str", ""),
    "correctness.openrouter_llm_model":          ("correctness", "str", "openai/gpt-4o-mini"),
    "correctness.openrouter_llm_model_fallback": ("correctness", "str", ""),
    # Local whisper.cpp transcription — see Config.use_local_whisper's docstring for exactly
    # what this does and doesn't replace. Binary defaults to where the Dockerfile bakes it; model
    # defaults to WHISPER_MODEL (docker-compose env var, default "small.en-q5_1" — see
    # DEFAULT_LOCAL_WHISPER_MODEL_PATH), downloaded on first use if it isn't already baked into
    # the image. Only relevant once use_local_whisper is turned on.
    "correctness.use_local_whisper":     ("correctness", "bool", True),
    "correctness.local_whisper_binary":  ("correctness", "str", "/usr/local/bin/whisper-cli"),
    "correctness.local_whisper_model":   ("correctness", "str", str(DEFAULT_LOCAL_WHISPER_MODEL_PATH)),
    "correctness.local_whisper_use_gpu": ("correctness", "bool", True),
    "correctness.local_whisper_threads": ("correctness", "int", 4),
    "correctness.require_audio_lang":       ("correctness", "str", "en"),
    # off | quarantine | blacklist | remediate — what to do with a file the correctness check
    # flags SUSPECT (see Config.correctness_auto_action).
    "correctness.auto_action":              ("correctness", "str", "off"),

    # Generate missing subtitles (see generate.py) -- own settings group, deliberately separate
    # from correctness.* even where the provider choice overlaps (groq/openrouter), so a user
    # can pick different providers/keys/models for the cheap sampled correctness check vs. the
    # much heavier full-track generation job.
    "generate.enabled":                          ("generate", "bool", False),
    "generate.stt_provider":                     ("generate", "str", "groq"),  # groq | openrouter | cloudflare
    "generate.groq_api_key":                     ("generate", "str", ""),
    "generate.groq_stt_model":                   ("generate", "str", "whisper-large-v3"),
    "generate.groq_stt_model_fallback":          ("generate", "str", "whisper-large-v3-turbo"),
    "generate.openrouter_api_key":                ("generate", "str", ""),
    "generate.openrouter_stt_model":              ("generate", "str", "openai/whisper-large-v3"),
    "generate.openrouter_stt_model_fallback":     ("generate", "str", ""),
    "generate.cloudflare_account_id":             ("generate", "str", ""),
    "generate.cloudflare_api_token":              ("generate", "str", ""),
    "generate.cloudflare_stt_model":              ("generate", "str", "@cf/openai/whisper-large-v3-turbo"),
    # Seconds of (compressed) audio per STT request -- per provider, since each has a different
    # practical per-request size/duration limit (Cloudflare's is undocumented and empirically
    # much smaller -- see generate.py's module docstring). Confirm/tune against your own account
    # before relying on the default for Cloudflare.
    "generate.chunk_seconds_groq":                ("generate", "int", 600),
    "generate.chunk_seconds_openrouter":          ("generate", "int", 600),
    "generate.chunk_seconds_cloudflare":          ("generate", "int", 60),
    "generate.audio_bitrate_kbps":                ("generate", "int", 64),
    "generate.assume_spoken_lang":                ("generate", "str", ""),
    # Whisper vocabulary hint (Groq/OpenRouter only) -- short plain comma-separated list, e.g.
    # "Jeff, Britta, Abed, Troy, Annie, Shirley, Pierce, Chang". Flattened to one line and capped
    # at VOCABULARY_HINT_MAX_CHARS on save AND on read (clean_vocabulary_hint) -- see that
    # constant for why the SHAPE of this field, not just its content, matters.
    "generate.vocabulary_hint":                   ("generate", "str", ""),
    "generate.llm_provider":                      ("generate", "str", "groq"),  # groq | openrouter | gemini
    "generate.groq_llm_model":                    ("generate", "str", "openai/gpt-oss-20b"),
    "generate.groq_llm_model_fallback":           ("generate", "str", "allam-2-7b"),
    "generate.openrouter_llm_model":              ("generate", "str", "openai/gpt-4o-mini"),
    "generate.openrouter_llm_model_fallback":     ("generate", "str", ""),
    "generate.gemini_api_key":                    ("generate", "str", ""),
    "generate.gemini_llm_model":                  ("generate", "str", "gemini-2.0-flash"),
    "generate.gemini_llm_model_fallback":         ("generate", "str", ""),
    "generate.translate_batch_size":              ("generate", "int", 20),
    # Caps DISTINCT VIDEOS generated per rolling 24 hours -- see Config.generate_max_videos_per_day.
    "generate.max_videos_per_day":                ("generate", "int", 3),

    "automation.remediate_max_attempts":   ("automation", "int", 3),
    # 0-100 (Bazarr's own percentage score for a candidate, NOT our correctness check) — 0 =
    # disabled. A remediation candidate scoring below this is skipped and never even downloaded.
    "automation.remediate_min_score":      ("automation", "float", 80.0),
    "automation.dry_run":                  ("automation", "bool", False),

    "bazarr.url":       ("bazarr", "str", ""),
    "bazarr.api_key":   ("bazarr", "str", ""),
    "bazarr.path_map":  ("bazarr", "list_pairs", []),

    # How chatty the app's own log is (applog.py/db.app_log_lines — see Settings -> Log's
    # viewer). Applied to the actual Python logger immediately on save (see
    # web/routers/settings.py), not just stored inertly.
    "log.level": ("log", "str", "INFO"),

    "scheduling.cron":         ("scheduling", "str", "0 4 * * 0"),
    "scheduling.run_on_start": ("scheduling", "bool", False),
    # On by default — see bazarr_poll.py. Polls Bazarr's own "wanted" lists, no Sonarr/Radarr
    # connection needed at all (removed — Bazarr's history already carries everything blacklist/
    # remediate need, and this is a strictly better "is it ready" signal than anything Sonarr/
    # Radarr's own API gave us).
    "scheduling.poll_new_media_enabled":           ("scheduling", "bool", True),
    "scheduling.poll_new_media_interval_minutes":  ("scheduling", "int", 10),
    "scheduling.poll_library_enabled":             ("scheduling", "bool", True),
    "scheduling.poll_library_interval_minutes":    ("scheduling", "int", 720),  # 12 hours
}
# Keys whose value is never returned in plaintext to the frontend (only "is_set: true/false").
SECRET_KEYS = {
    "correctness.groq_api_key", "correctness.openrouter_api_key", "bazarr.api_key",
    "generate.groq_api_key", "generate.openrouter_api_key", "generate.cloudflare_api_token",
    "generate.gemini_api_key",
}

GROUPS = sorted({g for g, *_ in SETTING_DEFS.values()})


def _serialize(kind: str, value):
    import json
    if kind == "bool":
        return "1" if value else "0"
    if kind == "list":
        return json.dumps(list(value))
    if kind == "list_pairs":
        return json.dumps([list(p) for p in value])
    return "" if value is None else str(value)


def _deserialize(kind: str, raw: Optional[str], default):
    import json
    if raw is None:
        return default
    if kind == "bool":
        return raw == "1"
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind in ("list", "list_pairs"):
        # Same JSON-array storage for both (see _serialize) — the pair structure is just
        # a convention on top, not a separate encoding.
        return json.loads(raw) if raw else []
    return raw


def get_all_settings(conn) -> dict:
    """Typed dict {key: value} for ALL known settings — a missing row in the DB falls back
    to SETTING_DEFS' default, so a fresh system always has a full, usable Config."""
    from verifyarr import db
    raw = db.get_all_settings_raw(conn)
    out = {}
    for key, (_group, kind, default) in SETTING_DEFS.items():
        out[key] = _deserialize(kind, raw.get(key), default)
    return out


def get_settings_group(conn, group: str, redact_secrets: bool = True) -> dict:
    if group not in GROUPS:
        raise KeyError(f"unknown settings group: {group}")
    all_vals = get_all_settings(conn)
    result = {}
    for key, (g, _kind, _default) in SETTING_DEFS.items():
        if g != group:
            continue
        short = key.split(".", 1)[1]
        if redact_secrets and key in SECRET_KEYS:
            result[short] = {"is_set": bool(all_vals[key])}
        else:
            result[short] = all_vals[key]
    return result


def set_settings_group(conn, group: str, values: dict) -> None:
    """Updates only the keys in `values` that actually belong to this group — unknown keys
    are ignored rather than failing the whole call, so the frontend can send the whole form
    without knowing about fields that may have been removed. A secret key sent as None/empty
    does NOT change the stored value (so the UI doesn't need to re-send it to save the rest
    of the group) — pass an explicit empty string to actually clear it."""
    from verifyarr import db
    for short, value in values.items():
        key = f"{group}.{short}"
        if key not in SETTING_DEFS or SETTING_DEFS[key][0] != group:
            continue
        if key in SECRET_KEYS and value is None:
            continue
        if key == "bazarr.url":
            value = normalize_url(value)
        elif key == "generate.vocabulary_hint":
            value = clean_vocabulary_hint(value) or ""
        elif key == "generate.assume_spoken_lang":
            cleaned = clean_lang_code(value)
            if value and not cleaned:
                raise ValueError(f"not a language code: {value!r} — use a short code like 'en' or 'da'")
            value = cleaned
        _group, kind, _default = SETTING_DEFS[key]
        db.set_setting_raw(conn, key, _serialize(kind, value))


# --- one-time best-effort import of old env-vars into the settings table ------------------------

_ENV_IMPORT_MAP = {
    # general.media_roots (list) and sync.alass_bin are gone (see Config) — there's no clean
    # env-var predecessor for movies_folder/series_folder, so they're deliberately not imported.
    "general.subtitle_langs":               ("SUBTITLE_LANGS", "list"),
    "log.level":                             ("LOG_LEVEL", "str"),
    "sync.split_penalty":                   ("ALASS_SPLIT_PENALTY", "int"),
    "sync.min_change_seconds":              ("MIN_CHANGE_SECONDS", "float"),
    "sync.sample_count":                    ("CORRECTNESS_SAMPLE_COUNT", "int"),
    "sync.clip_seconds":                    ("CORRECTNESS_CLIP_SECONDS", "int"),
    "sync.window_minutes":                  ("CORRECTNESS_WINDOW_MINUTES", "float"),
    "sync.overlap_threshold":               ("CORRECTNESS_OVERLAP_THRESHOLD", "float"),
    "correctness.enabled":                  ("ENABLE_CORRECTNESS_CHECK", "bool"),
    "correctness.groq_api_key":             ("GROQ_API_KEY", "str"),
    "correctness.groq_model":               ("GROQ_MODEL", "str"),
    "correctness.groq_llm_model":           ("GROQ_LLM_MODEL", "str"),
    "correctness.groq_llm_model_fallback":  ("GROQ_LLM_MODEL_FALLBACK", "str"),
    "correctness.require_audio_lang":       ("CORRECTNESS_REQUIRE_AUDIO_LANG", "str"),
    # The old single AUTO_ACTION_ON_SUSPECT still seeds this, so an upgrading user's behavior is
    # preserved; it used to seed a line-order action too, which no longer exists.
    "correctness.auto_action":              ("AUTO_ACTION_ON_SUSPECT", "str"),
    "automation.remediate_max_attempts":    ("REMEDIATE_MAX_ATTEMPTS", "int"),
    "automation.dry_run":                   ("DRY_RUN", "bool"),
    "bazarr.url":                           ("BAZARR_URL", "str"),
    "bazarr.api_key":                       ("BAZARR_API_KEY", "str"),
    "bazarr.path_map":                      ("PATH_MAP", "list_pairs"),
    "scheduling.cron":                      ("SWEEP_CRON", "str"),
    "scheduling.run_on_start":              ("RUN_ON_START", "bool"),
}


def import_from_env_once(conn) -> bool:
    """If the settings table is completely empty AND relevant env-vars exist in the
    container's environment, settings are seeded from them once (marked with
    `_migrated_from_env=1`), so upgrading from the old env-var-only setup doesn't require
    re-entering everything in the UI. Never touches anything once settings already exist
    (whether from this import or later UI edits) — called on every startup, but effectively
    only active the first time."""
    from verifyarr import db
    if db.get_setting_raw(conn, "_migrated_from_env") is not None:
        return False
    if not db.get_all_settings_raw(conn):
        seeded = False
        for key, (env_name, kind) in _ENV_IMPORT_MAP.items():
            raw_env = os.environ.get(env_name)
            if raw_env is None:
                continue
            if kind == "list":
                value = _env_list(env_name, "")
            elif kind == "list_pairs":
                value = _parse_path_map("")
            elif kind == "bool":
                value = _env_bool(env_name, False)
            elif kind == "int":
                value = int(raw_env)
            elif kind == "float":
                value = float(raw_env)
            else:
                value = raw_env
            db.set_setting_raw(conn, key, _serialize(kind, value))
            seeded = True
        if seeded:
            db.set_setting_raw(conn, "_migrated_from_env", "1")
            return True
    db.set_setting_raw(conn, "_migrated_from_env", "1")
    return False


def migrate_log_level_once(conn) -> None:
    """log_level moved from the General tab to its own Log tab (log.level, was
    general.log_level) — carries over an already-chosen level so an upgrade doesn't silently
    reset it back to INFO. Idempotent, cheap enough to just call on every startup."""
    from verifyarr import db
    if db.get_setting_raw(conn, "log.level") is not None:
        return
    old = db.get_setting_raw(conn, "general.log_level")
    if old is not None:
        db.set_setting_raw(conn, "log.level", old)
