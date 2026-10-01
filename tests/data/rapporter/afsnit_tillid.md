# Episode trust: which episodes can be used as ground truth?

Method: the same metric as `verifyarr_handoff/oracle.py` (`global_offset`: the best constant
offset by word-time agreement, match window ±2.0 s, tokens ≥4 letters). The reference is
`out/<slug>.words.json` (large-v3-turbo); the subtitle is the `.en.srt`/`.en.hi.srt` file
that each fixture (`verifyarr/tests/fixtures/whisper_full/<slug>.json`, field `subtitle_name`) points to.
Sweep ±60 s (coarse 0.25 s + fine 0.05 s around the best). Residual/spread: the median residual per
third of the episode at the best offset (`spread` = max−min), the linear drift slope (s/min)
and the agreement per third (`3rd min`). A neighbour check and an offset sweep per third look for outliers.

Classes: **GROUND TRUTH** = suitable as ground truth · **CONTENT** = the right content, but out of sync
(usable for content tests, useless as timing ground truth) · **WRONG** = wrong content.
`Old` = the current BAD list in `min_coverage.py` (BAD/GOOD). `⚠` = deviates from it.

| Episode | Class | Old | Offset (s) | Agree best | Agree v0 | Spread (s) | Slope (s/min) | 3rd min | Note |
|---|---|---|---|---|---|---|---|---|---|
| C_S02E24 | GROUND TRUTH | GOOD | +1.60 | 0.805 | 0.722 | 0.20 | +0.014 | 0.79 |  |
| C_S02E13 | GROUND TRUTH | GOOD | +1.85 | 0.785 | 0.644 | 0.11 | +0.005 | 0.77 |  |
| C_S02E03 | GROUND TRUTH | GOOD | +1.75 | 0.783 | 0.649 | 0.07 | +0.005 | 0.75 |  |
| C_S03E19 | GROUND TRUTH | GOOD | +1.30 | 0.781 | 0.734 | 0.03 | -0.002 | 0.77 |  |
| C_S03E13 | GROUND TRUTH | GOOD | +1.10 | 0.780 | 0.716 | 0.11 | +0.008 | 0.76 |  |
| C_S03E03 | GROUND TRUTH | GOOD | +1.70 | 0.777 | 0.690 | 0.10 | +0.010 | 0.77 |  |
| C_S03E18 | GROUND TRUTH | GOOD | +1.75 | 0.775 | 0.697 | 0.25 | +0.010 | 0.74 |  |
| C_S02E12 | GROUND TRUTH | GOOD | +1.95 | 0.771 | 0.656 | 0.11 | -0.004 | 0.76 |  |
| C_S03E22 | GROUND TRUTH | GOOD | +1.45 | 0.771 | 0.727 | 0.13 | +0.009 | 0.74 |  |
| C_S02E09 | GROUND TRUTH | GOOD | +1.90 | 0.762 | 0.655 | 0.23 | -0.009 | 0.75 |  |
| C_S03E21 | GROUND TRUTH | GOOD | +1.30 | 0.761 | 0.721 | 0.05 | -0.005 | 0.75 |  |
| C_S03E17 | GROUND TRUTH | GOOD | +1.30 | 0.759 | 0.703 | 0.34 | +0.003 | 0.75 |  |
| SH_S01E02 | GROUND TRUTH | GOOD | +1.90 | 0.759 | 0.674 | 0.10 | -0.001 | 0.75 |  |
| C_S03E06 | GROUND TRUTH | GOOD | +2.10 | 0.756 | 0.618 | 0.10 | +0.002 | 0.73 |  |
| C_S03E09 | GROUND TRUTH | GOOD | +1.65 | 0.753 | 0.622 | 0.06 | -0.003 | 0.72 |  |
| C_S03E15 | GROUND TRUTH | GOOD | +1.45 | 0.751 | 0.719 | 0.06 | +0.005 | 0.73 |  |
| C_S02E02 | GROUND TRUTH | GOOD | +1.65 | 0.749 | 0.625 | 0.03 | +0.001 | 0.72 |  |
| C_S03E12 | GROUND TRUTH | GOOD | +1.70 | 0.748 | 0.604 | 0.14 | +0.007 | 0.72 |  |
| C_S02E10 | GROUND TRUTH | GOOD | +1.75 | 0.748 | 0.634 | 0.06 | +0.001 | 0.70 |  |
| SH_S01E06 | GROUND TRUTH | GOOD | +1.90 | 0.748 | 0.653 | 0.05 | -0.000 | 0.71 |  |
| SH_S01E04 | GROUND TRUTH | GOOD | +1.90 | 0.745 | 0.668 | 0.20 | +0.002 | 0.72 |  |
| C_S02E18 | GROUND TRUTH | GOOD | +1.20 | 0.744 | 0.722 | 0.07 | -0.001 | 0.73 |  |
| C_S02E07 | GROUND TRUTH | GOOD | +1.70 | 0.732 | 0.632 | 0.15 | +0.000 | 0.73 |  |
| SH_S01E03 | GROUND TRUTH | GOOD | +1.75 | 0.731 | 0.641 | 0.07 | +0.001 | 0.68 |  |
| SH_S01E01 | GROUND TRUTH | GOOD | +1.80 | 0.731 | 0.633 | 0.11 | +0.002 | 0.69 |  |
| C_S03E08 | GROUND TRUTH | GOOD | +1.40 | 0.728 | 0.658 | 0.19 | -0.008 | 0.72 |  |
| C_S02E05 | GROUND TRUTH | GOOD | +1.95 | 0.727 | 0.600 | 0.22 | +0.001 | 0.70 |  |
| C_S02E01 | GROUND TRUTH | GOOD | +1.85 | 0.725 | 0.593 | 0.16 | -0.010 | 0.69 |  |
| C_S03E10 | GROUND TRUTH | GOOD | +1.50 | 0.720 | 0.670 | 0.26 | +0.015 | 0.62 |  |
| SH_S01E05 | GROUND TRUTH | GOOD | +1.55 | 0.691 | 0.622 | 0.11 | +0.004 | 0.67 |  |
| C_S03E16 | CONTENT | GOOD | +1.60 | 0.771 | 0.638 | 0.52 | -0.007 | 0.75 | ⚠ Piecewise kink: middle third -0.82 against -0.30/-0.40 on the sides (spread 0.52). |
| C_S02E11 | CONTENT | GOOD | +1.90 | 0.760 | 0.590 | 0.76 | +0.051 | 0.75 | ⚠ Residual drift: third medians -0.83/-0.45/-0.07 (monotonic), slope +0.051 s/min. |
| C_S02E04 | CONTENT | GOOD | +1.70 | 0.745 | 0.610 | 0.91 | -0.061 | 0.68 | ⚠ Residual drift: third medians -0.15/-0.56/-1.06 (monotonic), slope -0.061 s/min. |
| C_S02E06 | CONTENT | GOOD | +1.00 | 0.711 | 0.669 | 0.75 | -0.050 | 0.68 | ⚠ Residual drift: third medians -0.05/-0.39/-0.80 (monotonic), slope -0.050 s/min. |
| C_S03E04 | CONTENT | GOOD | +1.35 | 0.710 | 0.620 | 0.76 | -0.048 | 0.69 | ⚠ Residual drift: third medians -0.05/-0.45/-0.81 (monotonic), slope -0.048 s/min. |
| C_S03E01 | CONTENT | BAD | +1.65 | 0.625 | 0.559 | 0.03 | -0.001 | 0.37 | ⚠ Partial: T1/T2 match at +1.5 s (0.81/0.74), T0 matches nowhere (best 0.43). |
| C_S03E11 | CONTENT | BAD | +1.60 | 0.600 | 0.561 | 0.23 | -0.015 | 0.28 | ⚠ Partial: T0/T1 at +1.5 s (0.74/0.76), T2 matches nowhere (best 0.52). |
| C_S03E14 | CONTENT | BAD | +1.90 | 0.547 | 0.437 | 0.39 | +0.000 | 0.07 | ⚠ Piecewise: T1/T2 at +1.5/+2.0 s (0.80/0.81), T0 at +17 s (0.74). |
| C_S03E07 | CONTENT | BAD | +24.25 | 0.500 | 0.018 | 0.18 | +0.035 | 0.08 | ⚠ Piecewise: T0/T1 at +24 s (0.69/0.75), T2 at +36.5 s (0.73). |
| C_S03E05 | CONTENT | BAD | -0.85 | 0.444 | 0.424 | 2.44 | +0.132 | 0.35 | ⚠ Linear drift +0.132 s/min (about 2-3 s over the episode), spread 2.44. |
| C_S03E02 | CONTENT | BAD | +19.20 | 0.439 | 0.019 | 0.09 | +0.001 | 0.01 | ⚠ Piecewise: T0/T1 at +19 s (0.71/0.48), T2 at -13.5 s (0.74). |
| C_S02E21 | CONTENT | BAD | +0.95 | 0.409 | 0.384 | 0.26 | -0.021 | 0.04 | ⚠ Piecewise: T0 matches at +1.0 s (0.78), T2 matches at +41.5 s (0.67). |
| C_S03E20 | CONTENT | BAD | -25.00 | 0.395 | 0.272 | 1.35 | +0.005 | 0.01 | ⚠ Piecewise: T0/T1 at -25 s (0.49/0.63), T2 at +1.5 s (0.63). |
| C_S02E08 | WRONG | BAD | -59.85 | 0.102 | 0.047 | 0.89 | -0.047 | 0.02 |  |
| C_S02E19 | WRONG | BAD | -35.85 | 0.070 | 0.048 | 1.35 | -0.035 | 0.01 |  |
| C_S02E20 | WRONG | BAD | -33.55 | 0.023 | 0.009 | 0.68 | +0.011 | 0.01 |  |
| C_S02E22 | WRONG | BAD | +5.75 | 0.022 | 0.014 | 1.49 | -0.004 | 0.02 |  |
| C_S02E16 | WRONG | BAD | -35.80 | 0.021 | 0.010 | 1.18 | +0.045 | 0.02 |  |
| C_S02E17 | WRONG | BAD | +46.00 | 0.020 | 0.014 | 0.84 | +0.007 | 0.01 |  |
| C_S02E14 | WRONG | BAD | -12.55 | 0.020 | 0.014 | 1.49 | -0.030 | 0.02 |  |
| C_S02E15 | WRONG | BAD | +60.10 | 0.020 | 0.011 | 0.58 | +0.016 | 0.01 |  |
| C_S02E23 | WRONG | BAD | -21.55 | 0.017 | 0.012 | 1.34 | +0.043 | 0.01 |  |

## Thresholds (set from the distribution, calibrated against Slow Horses)

The 6 Slow Horses episodes (the only user-confirmed ones) lie at: agree best 0.691–0.759,
offset +1.55…+1.90 s, agree v0 0.622–0.674, spread ≤0.20, |slope| ≤0.004, 3rd min ≥0.667.
The metric backs up the calibration: confirmed content scores high overall, so the thresholds
are set by them — not the other way around.

- **WRONG** (agree best < 0.30): the distribution is empty between 0.11 and 0.39. Everything below 0.11
  does not match the neighbouring episodes' references either (>0.11 nowhere) — these are not
  swapped files, but foreign content. The nearest above the limit is C_S03E20 (0.395) with
  documented partial matches — correctly placed above the limit.
- **GROUND TRUTH**: agree best ≥ 0.65 (an empty interval 0.625–0.691 above the SH minimum; the same bar as the
  old work, so the numbers can be compared), |offset| ≤ 3 s, drop best→v0 ≤ 0.20,
  spread ≤ 0.40 (healthy bulk ≤ 0.34, next 0.52), |slope| ≤ 0.025 (bulk ≤ 0.015, drift ≥ 0.048),
  3rd min ≥ 0.60.
- The rest is **CONTENT**: the right content in one or more places in the file, but the timing does not hold.

Systematic bias: ALL ground-truth episodes — including Slow Horses — have a best offset of +1…+2 s
and third medians around −0.4…−0.8 s. It is a common-mode bias between Whisper word times
and subtitle timings, not a per-file sync error. Consequence: use the ground-truth files as they lie,
do not move them by −1.7 s; the bias cancels out in comparisons across episodes.

## Deviation from the current BAD list

The BAD list declares 17 episodes bad. The numbers say:

- 9 are genuinely wrong (agree ≤ 0.10, no neighbour match): C_S02E08, E14, E15, E16, E17, E19,
  E20, E22, E23. Here the BAD list holds.
- 8 have predominantly right content and are misclassified as unusable: C_S02E21, C_S03E01,
  C_S03E02, C_S03E05, C_S03E07, C_S03E11, C_S03E14, C_S03E20 — all with partial agreement
  of 0.44–0.78 at piecewise offsets (drift, +19/+24/−25 s shifts or a single defective act).
- Conversely the list declares 5 episodes good that the numbers do not back up as timing ground truth:
  C_S02E04, C_S02E06, C_S02E11, C_S03E04 (residual drift ~0.8–0.9 s monotonic over the episode) and
  C_S03E16 (a piecewise kink of ~0.5 s in the middle act). No BAD episode qualifies as GROUND TRUTH.

## Conclusion: which episodes should the matrix use as ground truth?

Yes — there are plenty. 30 of 52 episodes are suitable as ground truth (24 Community + all 6 Slow Horses):

C_S02E01, E02, E03, E05, E07, E09, E10, E12, E13, E18, E24, C_S03E03, E06, E08, E09, E10,
E12, E13, E15, E17, E18, E19, E21, E22, SH_S01E01–E06.

For 10 episodes, the proposal is the 6 Slow Horses (user-confirmed) + the 4 Community with the highest margin,
e.g. C_S02E24 (0.805), C_S02E13 (0.785), C_S02E03 (0.783), C_S03E19 (0.781) — all with spread
≤ 0.20 and 3rd min ≥ 0.72. The 5 GOOD-but-not-ground-truth episodes (E04, E06, E11, S03E04, S03E16)
should drop out as timing ground truth until they are measured against a synced version of the subtitle (a linear
re-sync per episode removes the drift; E16 needs a piecewise re-sync of the middle act).
C_S03E05 (drift +0.13 s/min) is the model example of an episode where the recovery measurement today
penalises the pipeline for syncing to the audio: after a re-sync it belongs in CONTENT→GROUND TRUTH.
The weak drift result can thus partly be a ground-truth problem, not a pipeline problem —
repeat the drift measurement on the 10 proposed ones before concluding about the pipeline.
