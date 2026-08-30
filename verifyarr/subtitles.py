"""Subtitle parsing/comparison (srt/ass/ssa/vtt via pysubs2) + tokenizing for the
correctness check."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

try:
    import pysubs2
except ImportError:  # pragma: no cover
    print("Missing the 'pysubs2' package. Run: pip install pysubs2", file=sys.stderr)
    raise

WORD_RE = re.compile(r"[a-zA-ZæøåÆØÅéèêëüöäßñçÉÈÊËÜÖÄ']{4,}")

# Common English filler words (>=4 chars, anything shorter is already excluded by WORD_RE)
# excluded from the correctness check's word overlap — see tokenize(). Without this, two
# completely unrelated dialogue excerpts often score 0.4-0.6 overlap by pure chance, since
# ordinary spoken-English grammar (were/that/about/come/still ...) alone gives a big overlap
# regardless of content — verified against a real mismatched subtitle where the score was
# 0.625 for an excerpt that was actually from a different episode entirely.
STOPWORDS = {
    "your", "yours", "yourself", "yourselves", "this", "that", "these", "those", "been", "being",
    "having", "doing", "and", "but", "because", "until", "while", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "from", "down", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "both", "each", "more", "most",
    "other", "some", "such", "only", "same", "than", "very", "will", "just", "don't", "should",
    "should've", "now", "aren't", "couldn't", "didn't", "doesn't", "hadn't", "hasn't", "haven't",
    "isn't", "mustn't", "needn't", "shouldn't", "wasn't", "weren't", "won't", "wouldn't", "yeah",
    "okay", "well", "like", "know", "gonna", "wanna", "gotta", "right", "really", "actually",
    "guess", "mean", "said", "says", "tell", "told", "look", "looks", "looking", "going", "come",
    "came", "still", "about", "something", "someone", "anything", "everything", "nothing", "thing",
    "things", "people", "guys", "they", "them", "their", "theirs", "what", "which", "were", "have",
    "with", "would", "could", "also", "even", "much", "many", "you're", "you've", "you'll", "you'd",
    "she's", "hers", "herself", "himself", "itself", "themselves", "whom",
}


def load_subs(path: Path) -> "pysubs2.SSAFile":
    last_err = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pysubs2.load(str(path), encoding=enc)
        except (UnicodeDecodeError, pysubs2.exceptions.Pysubs2Error) as e:
            last_err = e
            continue
    raise ValueError(f"Could not parse {path} ({last_err})")


def subs_fingerprint(subs: "pysubs2.SSAFile") -> str:
    """Stable hash of a subtitle's actual content (event timings + text) — lets a caller tell
    whether a subtitle has changed since a previous Whisper-based check, so expensive results from
    then (see line_order.py's collect_samples cache) can be safely reused instead of re-running
    Whisper. Deliberately content-based, not the file's mtime/size — those change even on a
    no-op re-sync (same timings, rewritten file), which would otherwise throw away a still-valid
    cache for nothing."""
    h = hashlib.sha256()
    for e in subs.events:
        h.update(f"{e.start}|{e.end}|{e.text}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def max_shift_stats(old: "pysubs2.SSAFile", new: "pysubs2.SSAFile"):
    old_ev, new_ev = list(old.events), list(new.events)
    n = min(len(old_ev), len(new_ev))
    if n == 0:
        return None, None, len(old_ev), len(new_ev)
    diffs = [abs(new_ev[i].start - old_ev[i].start) / 1000.0 for i in range(n)]
    return max(diffs), sum(diffs) / n, len(old_ev), len(new_ev)


def tokenize(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text or "")} - STOPWORDS


def subs_text_in_window(subs: "pysubs2.SSAFile", center_sec: float, before_sec: float, after_sec: float) -> str:
    lo_ms, hi_ms = (center_sec - before_sec) * 1000, (center_sec + after_sec) * 1000
    return "\n".join(e.plaintext for e in subs.events if lo_ms <= e.start <= hi_ms)


def pick_dialogue_dense_time(subs: "pysubs2.SSAFile", region_start: float, region_end: float,
                              window_sec: float) -> float:
    """Within [region_start, region_end] (seconds), pick the clip start time whose window_sec-long
    window contains the most subtitle dialogue (by character count) — avoids landing a correctness
    sample on a silent or dialogue-free stretch (action, score, an explosion) just because it
    happened to be the region's structural midpoint. Candidate start times are every subtitle
    event's own start within the region (checking every possible offset isn't worth it — dialogue
    density only meaningfully changes at event boundaries). Falls back to the region's midpoint
    when there's no dialogue anywhere in the region at all (nothing better to do there)."""
    lo_ms, hi_ms = region_start * 1000, region_end * 1000
    in_region = [e for e in subs.events if lo_ms <= e.start <= hi_ms]
    if not in_region:
        return (region_start + region_end) / 2
    best_start, best_score = None, -1
    for e in in_region:
        start = e.start / 1000.0
        window_hi_ms = (start + window_sec) * 1000
        score = sum(len(ev.plaintext) for ev in subs.events if start * 1000 <= ev.start < window_hi_ms)
        if score > best_score:
            best_score, best_start = score, start
    return best_start
