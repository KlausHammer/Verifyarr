# Test matrix conclusion: 15 models × 10 episodes × 6 scenarios × 2 modes × 2 audio-confirm

Dataset: `tests/e2e_matrix.jsonl` — **3600 rows, 3552 ok, 48 skipped, 0 errors**,
run as 150 (episode × model) shards with 16 workers. The 48 skipped are two
sweep combinations, not pipeline failures: `medium.en-q5_0 / SH_S01E03` is missing
entirely (a known upstream whisper.cpp crash), and `base.en-greedy-cpu / SH_S01E05`
contains one invalid byte (`0x8f` in the transcription text, about 16:36), so the file
cannot be UTF-8 decoded. Both drop out as `skipped`, and both must be rerun once
the sweep data is fixed upstream. Summary: `tests/e2e_matrix_summary.json`.

Reading guide: every point has the number it rests on, and an uncertainty note.
"le1s" = the share of cues with a residual error ≤ 1.0 s after sync.

## A. Code changes — something only code can fix

### A1. Drift (2 % stretch) needs a rate/scale correction, not more piecewise offsets
- Full: le1s **0.32**. Sampled (the production default): le1s **0.055** — drift
  is in practice not handled at all in the setup that ships.
- The failure is the same on all episodes (full le1s 0.17–0.42) and all 15 models
  (0.77–0.82 overall) — it is the pipeline mechanics, not the evidence quality.
- Escalation detects the problem (fires on 224/296 sampled drift rows) but does not
  rescue it: escalated rows end at le1s 0.047. The full transcript
  does not help when the fixer can only do piecewise offsets.
- Conclusion: build a rate component (estimate the stretch factor, not only offsets).
  More clips do not solve it — see B3.

*Uncertainty: only one drift rate (2 %) and one mechanism (linear stretch) were tested.
The genuine drift case C_S03E04 (−0.048 s/min) is not detected as drift at all
(60/60 `already in sync`, 0 SUSPECT on clean) — the detection floor lies
above genuine small drift, so do not chase sub-0.05 s/min.*

### A2. The swap fixer restores too little and touches too much — recalibrate `_judge_order`
- Injected swaps restored (audio on): full **0.40**, sampled **0.15**.
  With audio off: 0.00 — expected, since off only flags.
- At the same time the fixer in full touches **21.2 non-injected cues per run**, and the
  clean→swap delta is only a median **+2**: the injected swaps drown in
  rewrites the fixer would have made anyway.
- The cross-model agreement over fixed indices is down at Jaccard
  **0.02–0.12** on Community with 15 models (the validation's ~0.4 with 3 models
  did not hold as N grew) — the models agree on the heuristic
  (the flagged set with audio off is identical, Jaccard 1.0 everywhere), but
  the audio confirmation diverges. The noise sits in `_judge_order`
  (`SWAP_MARGIN = 0.15`) and/or in the threshold (min. 3 confirmed, ≥ 30 %),
  not in the candidate finding.
- Conclusion: tighten `_judge_order` before auto-fixing more — see B1.

*Uncertainty: only one swap type (in-cue L1/L2 reversal, n = 6) was tested. That
"non-injected fixes" = false positives presumes the Community files are
healthy — see B2.*

### A3. Line-order false positives are confirmed — the same root cause as A2
- See the A2 numbers: 21 innocent rewrites per full run, Jaccard 0.02–0.12.
  It is not a separate finding, but the same calibration error seen from the
  clean side. No stand-alone fix; solve A2.

## B. Setup / flow — the code is fine, the setting is wrong

### B1. Keep `line_order_audio_confirm = False` as the default (do not switch to True)
- With `on`, 40–61 cues per Community episode are auto-fixed in full — but the models
  disagree on which ones (Jaccard 0.02–0.12). They cannot all be genuine errors in
  the file; the bulk are model artifacts. A default of `True` would silently
  rewrite dozens of innocent cues per file.
- With `off`, nothing is fixed, and the flagged sets are **identical across
  all 15 models** (Jaccard 1.0) — the review queue is deterministic and stable:
  7–19 cues per Community episode, 0–9 per Slow Horses episode.
- Conclusion: default `off` (flag-only) until A2 is solved. Reconsider only
  when the cross-model agreement over fixed indices is close to 1, not 0.1.

*Uncertainty: the recommendation protects against false positives at the expense of genuine
findings — the 2–11 unanimous cues per Community episode are probably genuine
and today are only flagged, not fixed.*

### B2. The Community-vs-Slow Horses difference sits in the files, not in the HI source
- Clean full.on: Community **40–61** auto-fixes per episode, Slow Horses **0–2**.
  The difference holds on all 15 models and on both `.en` and `.en.hi` files
  (C_S03E10 is `.hi` with 45 fixes; SH `.en` files get ~1) — so it is not
  HI subtitles or the source.
- The mechanics: the Community files contain far more two-line cues that
  trip the `_cap_signal` heuristic. Whether they are genuine errors or noise is settled
  by the A2 numbers: the agreement is 0.02–0.12, so the bulk is noise.
- Conclusion: listen through a sample of the unanimous cues (2–11 per episode)
  before the Community fixes are read as genuine findings. No code change.

*Uncertainty: 4 Community against 6 Slow Horses episodes; series style (dialogue density,
 punctuation) is not separated from file quality.*

### B3. Keep `sample_count = 5`, `clip_seconds = 60` — drift excepted
- Piecewise sampled: le1s 0.68, and escalated rows reach 0.73 against 0.49 for
  non-escalated — 5×60 s + escalation carries piecewise.
- Drift sampled: 0.055 regardless of escalation (A1). Five more clips would hit the same
  wall: the problem is missing rate estimation, not coverage.
- Escalation never fires wrongly: 0/1184 sampled rows on
  clean/uniform/swap/gap. It is free to keep.
- Conclusion: do not touch the sampling numbers; solve drift in the code (A1).

*Uncertainty: the sampled clips are cut out of the full transcript, not
newly transcribed — genuine short-clip noise is not measured.*

### B4. Asymmetric sync thresholds: don't — there is nothing to gain
- On 592 clean rows alass suggests **zero** shifts at all
  (neither +0.4 nor −0.4 s). `min_change_seconds` (0.25 s) and
  `ANCHOR_RESYNC_MIN_SHIFT_S` (1.0 s) never bite on healthy files, so a
  higher "shift later" limit would not change a single clean decision.
- Conclusion: keep symmetric thresholds. The cost of small positive shifts
  is hypothetical; the cost of drift (A1) is measured.

### B5. Model choice: the cheapest is good enough — take `tiny.en-greedy-cpu` / `small.en-greedy`
- Sync quality (le1s over all timing scenarios): all 15 models between
  **0.77 and 0.82**. The largest (medium.en, 0.80) does not beat the smallest
  (tiny.en-greedy-cpu, **0.82**, best in the field). Swap restore varies
  0.11–0.17 with no relation to size. Even the turbo fixture
  (large-v3-turbo, 0.79) is matched by local models.
- Run times (`sweep/tider_{gpu,cpu}.csv`, rc = 0): fastest on GPU is
  small.en-greedy (**44 s** average) and turbo-q5_0/q8_0 (61 s); fastest on CPU is
  tiny.en-greedy-cpu (**102 s** average) against e.g. base.en-cpu (193 s).
- Conclusion: run `small.en-greedy` where there is a GPU, `tiny.en-greedy-cpu`
  where there is only CPU. Large models buy nothing measurable.

*Uncertainty: the times are transcription time per episode on the sweep machine, not
this machine; the quality is the pipeline end result, not transcript WER —
a model with better word times could still help A1/A2 indirectly.*

## C. Not a problem — it works, don't chase it

- **Uniform (+45 s) and gap (5 min cut out): le1s 1.00 in both modes, all
  models.** There is no spread to optimise on.
- **Clean timing:** p50 0.0 s, le1s 1.00 in both modes — the pipeline does not touch
  healthy files' timing (untouched: full 185/296, sampled 241/296; the rest are
  line-order flags, not timing).
- **Escalation:** fires only where there is something to gain (drift 224/296,
  piecewise 206/296) and never on clean/uniform/swap/gap. The mechanics are sound;
  it is the drift fixer it escalates to that is missing (A1).
- **The reservations from the handoff hold and should not be built:** background speech
  (the estimate at the ceiling, median −0.06 s, the best filter AUC ~0.70 discards more
  than it helps), song lyrics (10.3 % error rate but only 4.5 % of all errors,
  concentrated in two numbers; HI song↔song anchors flawless 30/30), and
  clip granularity (5×60 s holds; ~2 % confident clips with |median| 1.0–1.5 s
  lie below SUSPECT 2.5 s and escalation 5.0 s). Raw data in
  `whisper_gpu_staging/verifyarr_handoff/`.

## Reservations for the whole document
- 10 episodes (4 Community + 6 Slow Horses): episode variation is real (drift
  full spans 0.17–0.42; piecewise 0.54–0.99 per episode) — small differences
  between models (< 0.05) are noise.
- One drift rate, one swap type, no combined scenarios in the defaults
  (`drift_swap` is opt-in and was not run here).
- Sampled clips are cut from sweep transcripts, not live short-clip STT.
- 48 skipped rows (2 sweep combinations, see the top) — rerun when upstream
  has delivered/fixed the files; `skipped` is not counted as done, so resume
  picks them up automatically.
