# Background speech as sync evidence (task E)

**Conclusion: don't build it.** Background speech exists (about 4 % of content-bearing
segments are fully uncovered, ~5 % of the playing time), and its mechanism accounts for
almost three quarters of the wrong anchors. But it cannot be detected
reliably with the data production has — the best available signal
(segment confidence) has an AUC of ~0.70, and all the filters tried discard more data
than they help: the anchor estimate is already at the ceiling (median −0.06 s,
MAD 0.31 s; at clip level 89/90 confident with a median |shift| of 0.19 s, unchanged by
all filters). Word-level anchors make it decidedly worse (the median moves +0.66 s,
the share outside ±1 s rises 16 % → 27 %). A negative result with numbers behind it —
the details follow below. Raw data: `baggrundstale.json`.

Measured on all 30 ground-truth episodes (9 user-confirmed + 21 others; the results
are the same across them — CORE9/ALL30 below show it). 13,749 segments, of which
11,764 with content tokens; 7,352 accepted anchors.

## Important precondition: which offset is "in sync"?

The trust table's `best_offset_s` (+1.1…+2.1 s) is **not** a sync error — it is the
cue-start→word-middle lag: cues typically start ~1–2 s before the words in the middle of the cue are
spoken (verified: word agreement peaks at a shift ≥+1.7 s, while segment start vs.
nearest cue start has a median of −0.27…+0.21 s on all 30 episodes). The anchor comparison
(`segment_start − line_start`) is therefore computed against offset 0, with
"correct" = |residual| ≤ 2.5 s (`ANCHOR_SUSPECT_THRESHOLD_S`). Had I used the
trust offset, all residuals would have been biased by −1.7 s — that was my first
run's mistake, caught and fixed before the numbers here.

Method otherwise: `_match_segments_to_lines` replicated faithfully to the code (≥0.5
overlap, ≥2 shared, strictly `>` in line order so the earliest line wins on
a tie, one anchor per line from the earliest segment), but with candidates limited
to ±10 s (production uses clips of minutes — distant doppelgängers are measured
separately, see weakness 3). Coverage is span-based: a Whisper word is covered if the
same content token sits in a cue whose [start−1 s, end+1 s] contains the word time.

## The four weaknesses: confirmed or refuted

1. **The segment boundary is set by the background speech — partly confirmed, but the tail,
   not the middle.** Lead (segment start → first shared word) has a median of 0.70 s;
   66 % ≥ 0.5 s, 32 % ≥ 1.0 s, 16 % ≥ 1.5 s. But most of it is normal
   word delay inside the cue (lead median 0.7 s at a residual ≈ 0), not
   background. The dangerous tail exists: corr(lead, residual) = −0.76, and
   the lead ≥1.5 s group (16 % of anchors) has a residual median of −1.14 s, MAD 0.74 —
   and holds **193 of 266 wrong anchors (73 %)**. The mechanism is real, but
   overall only 3.6 % of anchors are wrong, and the median is untouched (−0.06 s).
2. **Short lines are vulnerable — refuted in practice.** The example does not hold:
   `tokenize("Yeah, right.")` = ∅ (both are stop words) — that line cannot anchor at
   all. Lines with 2 tokens: 3.9 % wrong against 3.3 % for lines with ≥4
   (AUC 0.518 = a coin toss). A tightened requirement (≥3 shared for small lines) discards
   42 % of all anchors without moving the MAD (0.309 → 0.306). Not recommended.
3. **No margin — confirmed, but a small effect.** Ties (margin 0): 14.4 % of
   anchors, error rate 4.7 % vs. 3.4 % without a tie (margin AUC 0.591). Distant
   doppelgängers (the same/good overlap >10 s away — what production's wide window
   would see): 5.5 % of anchors with an 8.9 % error rate (2.5× the base rate), but they cover
   only 14 % of the wrong ones. A margin requirement removes 14 % of data for a MAD of 0.309 → 0.304.
   Marginal — can be taken or left.
4. **Confidence is not used — the signal exists, but is not worth using.**
   Segment confidence separates (AUC 0.70 at anchor level / 0.73 at segment level;
   covered vs. uncovered word median 0.92 vs. 0.70), and `conf ≥ film median − 0.1` is
   the best cheap filter (removes 27 % of the wrong ones for an 8 % data loss,
   std 1.11 → 0.99). But the clip estimate does not move (see C). Note:
   `no_speech_prob`/`avg_logprob`/`compression_ratio` do **not** exist in the
   out data (whisper.cpp has only the token `p`); in production they are preserved via
   `_normalize_segment` if the provider supplies them — but there is nothing to
   gain, the estimate is at the ceiling. A series difference is real, by the way (Community
   median 0.885 vs. Slow Horses 0.838), so absolute thresholds would be skewed.

## A. The size of the problem

- Fully uncovered segments (cov = 0): **499 (4.2 %)**, median duration 2.0 s,
  p90 5 s, max 30 s; total 1,826 s ≈ 61 s/episode ≈ **5 % of the playing time**.
  Examples: crowd/in-between lines with high confidence ("Someone asked you. You
  guys are being bitches.", conf 0.92; "Oh, sweetie, you don't owe us anything",
  conf 0.97) — confidently transcribed speech that the subtitle omits.
- Half uncovered (cov ≤ 0.5): 1,024 (8.7 %).
- 62.5 % of content-bearing segments become anchors; of them 16 % have
  lead ≥ 1.5 s (the weakness 1 tail above). The coverage label is diluted, by the way,
  by repeated lines (the same words in many cues): background-dominated anchors
  with cov ≤ 0.5 (n = 126) have a residual median of +0.01 s — they are not caught by
  coverage, but by lead (which requires word times).

## B. The signals — what separates, what is ineffective

AUC against "uncovered segment" (n = 11,764), at a sensible threshold precision/recall:

| Signal | AUC | Threshold | P / R | Verdict |
|---|---|---|---|---|
| word mean `pmean_c` (low = background) | 0.745 | < 0.8 | 0.23 / 0.56 | weak-moderate, best cheap |
| segment confidence (low = background) | 0.725 | < 0.7 | 0.35 / 0.27 | weak-moderate |
| `pmin_c` | 0.639 | < 0.5 | 0.13 / 0.64 | weak, too many false |
| speech density ratio (high = background) | 0.591 | > 2.0 | 0.09 / 0.85 | **ineffective** (almost everything above the threshold) |
| outside VAD speech | 0.585 | — | 0.11 / 0.70 | **ineffective** |
| surplus | 0.416 | — | — | **ineffective** (worse than a coin toss) |

AUC against "wrong anchor" (n = 7,352, 266 wrong): lead oracle 0.772
(**requires word times — unavailable in production**); seg confidence 0.700;
conf vs. film median 0.684; segment cov 0.615; margin 0.591; tie 0.523,
line length 0.518, VAD lead 0.487, ratio 0.320 (inverted) —
**all ineffective as classifiers**.

## C. What would filtering mean? (the decisive test)

Baseline overall residual: median −0.055 s, MAD 0.309 s, std 1.114 s.

| Filter | Dropped (of which wrong) | Median | MAD | Std |
|---|---|---|---|---|
| none (baseline) | — | −0.055 | 0.309 | 1.114 |
| require margin > 0 | 1,060 / 14 % (50) | −0.055 | 0.304 | 1.101 |
| require margin ≥ 0.2 | 1,169 (58) | −0.054 | 0.299 | 1.084 |
| ≥3 shared for small lines | 3,089 / **42 %** (121) | −0.067 | 0.306 | 1.082 |
| conf ≥ film median − 0.1 | 556 / 8 % (73) | −0.050 | 0.300 | 0.988 |
| VAD lead < 3 s | 1,329 (86) | −0.027 | 0.305 | 1.053 |
| ratio < 2.0 | 6,174 / 84 % (155) | −0.122 | 0.360 | 1.724 (worse!) |
| combo (margin + small line + ratio<3) | 5,425 / 74 % (174) | −0.085 | 0.327 | 1.263 |
| lead < 1.0 s (oracle, not buildable) | 2,431 (238) | +0.050 | 0.236 | 0.598 |

Clip level (emulated `_robust_clip_shift`: thirds, min. 3 anchors,
MAD ≤ 1.0): baseline **89/90 confident clips, median |clip median| 0.19 s** —
identical after the margin filter (90/90, 0.19 s) and the combo (89/90, 0.20 s).
**No filter improves the estimate: they are a loss of data.** A higher
`ANCHOR_MIN_SHARED_TOKENS` for short lines is explicitly not recommended (42 % loss,
no gain). A margin requirement is harmless but almost ineffective.

## D. Word-level anchors

First shared word instead of segment start (n = 7,260): MAD 0.303 → 0.323
(**not better**), median −0.05 → **+0.66 s systematic bias** (cue lead: the words
come after the cue start), share |resid| > 1 s 15.8 % → **27.2 %**. The median word
is worse (median +1.14 s). Std falls nicely (1.05 → 0.70), but the tails
are already clipped by the robust median/MAD estimator — the spread gain
cannot be cashed in, while the bias is real. **Negative: don't build.**

Realism: production's segments have no word times (`_normalize_segment`
keeps only start/end/text + confidence fields). The route is Groq
`timestamp_granularities: ["word"]` or a local whisper.cpp `-dtw`, plus
plumbing the word times through normalisation and the anchor match — moderate work
for a negative result. It is not commensurate with the gain (there is none).

## Caveats

- Only in-sync files: the interplay between background noise and a real sync error is not
  measured (it cannot by design be told apart here).
- Candidate window ±10 s vs. production's minute-wide clips: the real
  tie/doppelgänger rate in production may be higher; the full-window check
  (5.5 % distant doppelgängers) sets an upper bound.
- Coverage is diluted by repeated lines; "pure background" (4.2 %) is a
  conservative measure.
- The data is whisper.cpp large-v3-turbo with token `p`; production's Groq segments
  have other (richer) confidence fields, but the direction — a weak signal, the ceiling hit —
  is hardly changed by that.
- VAD silence as a sign of background is confounded by song (cf. DATAFORMAT).

## Reproduction

Analysis (scratch, not part of the deliverable): `/tmp/bg_analyse.py` (episode
modelling) + `/tmp/bg_eval.py` + `/tmp/bg_follow.py`, run with the project's
`.venv` python (pysubs2). Only read: `verifyarr/subtitles.py`
(`tokenize` imported directly), `generate.py` (`_CONFIDENCE_FIELDS`,
`_normalize_segment`), `out/`, fixtures, media subtitles. Nothing committed,
`out/`/`sweep/`/`wav/` untouched.
