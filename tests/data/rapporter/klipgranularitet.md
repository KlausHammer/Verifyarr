# Clip granularity: do E and F hold at 5 clips of 60 s?

**Conclusion: yes, both "don't build it" verdicts hold — with one clarification.** At
production's granularity (5 clips of 60 s, the same data/matching as E/F) no
buildable filter changes the estimate for the better; the margin requirement and the combo filter
make it decidedly worse. The clarification: E's third-level measurement showed no
false-positive clip medians, but at 5×60 s there are 2–4 confident clips out of
150 with |median| just above 1.0 s — all driven by E's lead tail (weakness 1),
high-confidence and without ties, so no buildable rule catches them. Only the
lead oracle (word times, unavailable in production) removes them. The risk is
bounded: all lie below 2.5 s (SUSPECT) and far below 5.0 s (escalation) —
the worst outcome is an unnecessary ~1 s rewrite, never a false escalation.
VAD placement does not seek out music (0/150 regions with more song than
dialogue density), and Slow Horses does not break (30/30 confident, 0 FP).
Raw data: `klipgranularitet.json`.

Method validation: E totals reproduced exactly (7,352 anchors, 266 wrong;
third-level sanity 89/90, median|shift| 0.189) and F totals exactly (7,470
anchors, 289 wrong; 41 seg-♪, 101 line-♪, 126 either; 17 HI/13 non-HI).
Placement is reimplemented faithfully to `pick_dialogue_dense_time` /
`pick_sample_time` (NUDGE 0,±10,±20,±30; min. 2.0 s of speech; cue overlap required),
5 equally wide regions over the video duration. Heuristic slots not emulated
(filler only). VAD timelines: `out/*.vad.tsv`.

## Part 1 — clip level at 5×60 s (E track, turbo)

Anchors per clip (dialogue strategy): min 5, p10 9, median 14, mean 14.2,
max 22. The task's fear (5–12 anchors, one wrong = 20 %) does not materialise:
**all 150 clips reach ANCHOR_MIN_COUNT = 3**, no clip has fewer than 5 anchors.
The VAD strategy is almost identical (mean 13.8); 0/150 VAD clips drop out as
"no evidence", and 88/150 VAD clips are bit-identical to the dialogue clip
(median nudge 0 s, mean 7.1 s).

| Strategy | Confident (MAD ≤ 1.0) | Dropout | FP (‖med‖ > 1.0) | Median ‖shift‖ | Spread med/max |
|---|---|---|---|---|---|
| dialogue density | 147/150 | 3 (all MAD 1.01–1.09) | 4 | 0.22 s | 0.42/1.37 s |
| VAD-nudged | 148/150 | 2 | 2 | 0.21 s | 0.42/1.23 s |

No file has a spread above 5.0 s (escalation is never triggered on in-sync files).

Filters at this granularity (all/150 clips; VAD in parentheses):

| Filter | Confident | FP | Spread med | Verdict |
|---|---|---|---|---|
| none (baseline) | 147 (148) | 4 (2) | 0.42 (0.42) | — |
| margin > 0 | 147 (144) | **7 (3)** | 0.51 (0.46) | worse: more FP, more dropouts |
| ≥3 shared, small lines | 149 (144) | 3 (0) | 0.64 (0.57) | ±1 FP for a 42 % data loss + larger spread — a loss of data |
| conf ≥ film median − 0.1 | 147 (147) | 3 (1) | 0.45 (0.41) | marginal, no estimate lift |
| combo (margin + small + ratio<3) | **100 (97)** | 6 (3) | 0.48 (0.42) | collapse: a third below the evidence limit |
| ♪ line excluded (F) | 146 (147) | 4 (2) | 0.42 (0.42) | literally no effect |
| lead < 1.0 (oracle) | 144 (145) | **0 (0)** | 0.29 (0.28) | the only one that works — requires word times, cannot be built |

The 4 dialogue FP clips (C_S03E08/0: −1.41; C_S03E13/1: −1.02; C_S03E17/2: −1.02
at MAD 0.14 and n=17; C_S03E21/0: −1.04): 6–13 of 9–17 anchors have lead ≥ 1.0 s
(median lead 1.1–1.7 s), all high-confidence, 0–2 ties, no ♪. It is E's
weakness-1 tail that at fine granularity pulls individual clip medians above
1.0 s — not a new mechanism. VAD's 2 FP are the same story (C_S03E17
again among them). The lead filter removes all FP, but in return pushes 5 clips below the
3-anchor limit — the oracle has a price too.

**E verdict at 5×60:** "don't build it" stands for all buildable filters. A nuance:
the third level hid that the lead tail gives ~2 % confident clips with
|median| ∈ [1.0; 1.5] s; that is still below any action threshold that
matters (2.5 s SUSPECT, 5.0 s spread).

F track (small.en, the same clips): baseline dialogue 145/150 confident, FP 4,
spread 0.61/max 4.57 (VAD: 146/150, FP 4). **V1/V2/V3 literally change
nothing** — the same confident, the same FP, the same spread to the last decimal.
The largest spread (SH_S01E06: 4.57) is small.en mistiming (17 agreeing anchors
~−4.5 s early in the file; turbo the same clip: −0.36), not music — and still
under 5.0. **F verdict at 5×60:** "don't build it" stands unchanged.

## Part 2 — does VAD seek out music and crowds? No

Paired per region (150 regions), VAD minus dialogue density: song seconds in the
clip −0.007 s (VAD has more song in **0/150** regions); uncovered word share
±0.000 (VAD higher in 33/150); speech +1.1 s/clip (VAD higher in 61/150 —
the nudge does what it should). Clips with any song at all: 5/150 dialogue (60
song seconds total) against 4/150 VAD (59 s). Anchor quality is equal or
marginally better with VAD (FP 2 against 4, confident 148 against 147). The theme number
(C_S03E03, 49 % of all song mass) gives fine clip medians (0.12–0.33 s) on
both strategies.

Why does the risk fail to appear? The nudge is max. ±30 s, **cue overlap is required**,
and the median nudge is 0 s — VAD only moves the clip when there is more speech near
the dialogue peak, not out into score/crowds. **The alternative (weighting against
subtitle coverage) is not necessary**; keep speech coverage alone.

## Part 3 — Slow Horses does not break

SH covers 9 % against Community's ~24 % (300 s of ~3,230 s vs. ~1,270 s), but
anchors per clip are almost equal (SH median 13 against C median 15/14):

| Series | Confident dialogue (VAD) | FP | Spread med/max |
|---|---|---|---|
| Community (120 clips) | 117 (118)/120 | 4 (2) | 0.44/1.37 |
| Slow Horses (30 clips) | **30 (30)/30** | **0 (0)** | 0.40/0.88 |

SH has zero dropouts, zero FP, and VAD placement changes nothing (song seconds
0.0 for both strategies; uncovered 0.069/0.071). If anything were to break, it would be
here — it does not.

## Caveats

- Same as E/F: only in-sync files; candidate window ±10 s (production's
  [start−30, start+90] window may give more distant doppelgängers — E's
  upper bound of 5.5 % still applies).
- Emulated transcription: E/F segments binned by segment start; genuine
  60 s clips transcribed with Groq segment differently.
- Heuristic slots not emulated; nor are extra slots on suspicion.
- The SH_S01E06 spread shows that model mistiming (here small.en) can give
  confident skewed clip medians — fixture-model-specific, not the production
  transcriber family.
