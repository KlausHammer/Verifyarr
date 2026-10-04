> **Archived.** Measured on earlier episode sets (Community, the Slow Horses set and the 11 approved episodes) with earlier code. The current facit is [`tests/known_good/`](../../tests/known_good/README.md) and [models and tests](../models_and_tests.md); numbers here are not quoted anywhere else.

# How well do the Whisper models work for sync and detection?

Measured on 52 episodes (Community S02+S03, Slow Horses S01) with large-v3-turbo as
ground truth, 14 cheaper configurations on 3 episodes, sparse sampling on all 52
and 4 Danish Stormester episodes. Machine-readable version: `modelrapport_ai.json`.

**The method in short:** every subtitle word is looked up in the Whisper word times; `agreement` is
the share found within ±2 s. The best offset is searched in ±60 s (0.5 s steps).
Everything below is measured — see **The gaps** at the end for what is missing.

## Model against model (agreement on E02/E03/E04 + sec./episode)

The times are a ranking, not benchmarks (the GPU and CPU workers ran at the same time).

| Model | Agreement | Time |
|---|---|---|
| turbo (reference) | 0.91–0.94 | — |
| turbo-q8_0 | 0.88–0.94 | 43 s |
| medium.en-greedy | 0.88–0.94 | ~60 s |
| small.en-greedy | 0.87–0.93 | ~30 s |
| small.en / q5_1 | 0.86–0.93 | ~55 s |
| base.en (±greedy) | 0.84–0.90 | ~100 s |
| tiny.en (±all) | 0.80–0.87 | ~70 s |

All 14 are viable (≥0.80). Quantisation costs almost nothing. Recommendation: `small.en-greedy`
on GPU, `tiny.en-q5_1`/`base.en-greedy` on CPU — N100 numbers are still missing.

## How many samples are needed?

Probe = 30 s transcript; offset per probe; truth = full-transcript block offset.
Hit = probe within ±2 s.

| Clips per 10 min | Hit |
|---|---|
| 1 | 0.953 |
| 2 | 0.994 |
| 3 | 0.981 |

**2×30 s is the sweet spot** (~10 % of the audio). The k=3 misses sit at jumps — they are
information, not noise. Guards: ≥40 words per probe, ≥20 cues, reject edge values.
Cheap models hold up on E02 (tiny too, though with more rejections at k=1).

## Detection: right, out of sync, or wrong?

The rule `file best < 0.65 OR probe spread > 5 s` gave **16/16 bad ones
flagged, 0/36 ok ones flagged** — a clean separation. The library contained all types:

- **Ok (36):** 0.84–0.96 at ~0 s.
- **Jump (5):** E21 (+40 s), S03E02 (+18/−15 s), S03E05, S03E07 — repairable.
- **Drift (1):** E08 — the right words, wandering offsets (framerate type).
- **Wrong content (8+):** the E14/E16/E20/E22 files contain a neighbouring episode's dialogue,
  and so do the E18/E19 backups. Agreement ≤0.15 at all offsets — quarantined, never synced.
- **Evidence:** S03E20 original 0.508 against CORRECTED-TEST 0.918.

Warning: bag-of-words overlap alone would mismatch (neighbouring episodes score 0.59–0.65 on a
shared vocabulary) — **timing agreement decides**.

## Sync over the whole file: alass with video or Whisper as the reference?

On E21 (the jump case), agreement per quarter:

- Original: 0.94 / 0.58 / 0.80 / 0.94 (the last ones need +40 s).
- alass+video: 0.35 / 0.37 / 0.40 / 0.52 — broken (5 confused blocks).
- alass+Whisper SRT: 0.94 / 0.94 / 0.90 / 0.95 — fixed.
- Direct block shift (without alass): 0.94 / 0.63 / 0.79 / 0.95 — coarser in a messy middle.

Clean files are left untouched by all methods (also with a `small.en` reference).
Conclusion: **keep alass, switch its reference to a Whisper SRT**; repair only
on a verified improvement (the E08/E14 types must never be overwritten).

## Danish (Stormester, 4/8 episodes)

Detection 8/8 Danish (p≈0.99) — but only on speech passages; the start of a file wrongly gave
English (p=0.69, music/intro). The transcript quality is excellent by eye, and 8
manual checkpoints in E01–E04 all lie within ~1 s. Machine agreement,
E05–E08 and `small` on Danish are missing (see The gaps). Danish needs its own word regex
with æøå.

## Adaptive densification (designed, UNTESTED)

k=3 does not beat k=2, because fixed dense probes more often land on top of a jump/silence.
Instead the flow escalates itself (`escalate()` in `localsync.py`, tested on
the E21 jump on 2026-09-15):
disagreeing neighbouring probes (>5 s) → one extra probe in between → repeat (max depth
3, max 8 extra clips). 4 probes over 20 min are enough for DETECTION; exact
placement is bought with 2–4 clips only around the jump. Ladder: k=2 screen →
escalation → full only on a flag → sequence match only on suspected swap.

## Line swap in cues: not measured

`swap_eval.py` (synthetic swapped two-line cues, sequence matching after 40 %
false positives with the median method) exists only as a `.pyc` with no results.
Neither alass nor word overlap can see a line swap — the track is unfinished.

## e2e: 52 episodes × 4 scenarios (measured 2026-09-15, the code with P0+VAD)

Healthy files: clean 35/35 ok with no false alarms · +45 s shift fixed on all
35 · line_swap caught on 29/35 healthy (1 miss: S03E19) + all 9 wrong ones
still SUSPECT. Wrong files: 40/47 untouched+SUSPECT, 7 rewritten-but-flagged
(backup switched on as the default). Jump files (8): all fixed via
anchor regions + ok. alass+Whisper beats alass+video on all 6 jump files
(video makes S03E02 worse: 18→37 s); on wrong files both fail correctly;
on clean files neither touches anything (±1 s). Details: `verifyarr_patches/`
(`e2e_merged.jsonl`, `alass_ref_after.json`).

## Non-uniform offsets (measured 2026-09-15, 8 healthy episodes, the code with P0+VAD)

The old e2e only tested +45 s on the whole file. Here are three uneven errors, seeded
(`offset-v1`, 8 parallel workers, no new Whisper — corruption on the SRT +
fixture shim):

- **Hole (5 min middle section deleted):** 8/8 perfect — 100 % of the surviving
lines ≤1 s (p50 ≤0.3 s), all flags ok. Good lines are not touched.
- **Drift (2 % progressive, fps type):** 8/8 flag ok, but loose: p50 ~1.0 s,
only ~47 % ≤1 s, ~80 % ≤2 s. Seen and corrected in broad strokes, stays ~1 s too loose.
- **Piecewise (6 blocks of ±5–15 s):** at first 5/8 good, 3/8 failed (C_S02E09,
C_S03E09, C_S03E16: p50 ~7 s) — all 3 flagged SUSPECT though, no silent failures.
Cause found: the anchor plan was vetoed by a single anchor per block boundary
— a clip that covers the boundary measures the median of two offsets (between the neighbours)
and triggered "thin region = the whole plan is rejected". Fix (patch 0004): straddlers
are dropped, the rest is verified densely as before. After the fix: **7/8 good** (p50 ≤0.5 s,
79–92 % ≤1 s); C_S03E09 partially (p50 7.1→1.5 s, 11→35 % ≤1 s) — dense blocks
(+8.1/+5.3 s) are over-segmented into 8 regions, the verification is blind at ~1–2 s.
The other 21 runs (drift/hole/healthy) byte-identical: no regression.
The before-measurement is saved as `e2e_offset_before_fix.jsonl`.

Conclusion: you were right that Whisper timestamps contain the answer — but
more transcriptions were not what was missing (55 anchors were already there); the fault was the
veto rule. Next step: C_S03E09's over-segmentation on dense blocks.
Details: `verifyarr_handoff/` (`e2e_offset.jsonl`, `e2e_offset.py`,
`e2e_offset_parallel.py`).

## The gaps (honestly)

Danish was measured by machine on all 8 Stormester episodes (large-turbo `-l da`):
agreement 0.81–0.85 at ~0 s, all ok, dtw_frac 1.0, mean_p ~0.83 (lower than
English ~0.91, as expected). Still open: `small`/medium on Danish · N100 times · Danish thresholds
(0.65/5 s are English-calibrated) · the S03E19 swap miss · the `-mc` test on only
2 files. The flow is ready to run in `localsync.py` (tested T1–T5).
