# Verifyarr Whisper / Sync / Line-Order Review — 2026-09-13

Produced by an independent Opus-model review agent, from a task brief covering production code,
this session's S02/S03/SlowHorses validation runs, and a Windows-GPU-generated large-v3-turbo
reference transcript of all 52 episodes (see
`/tmp/claude-1000/-home-hammer/037bfcdf-32e4-4d77-b54d-7c7879c6ad97/scratchpad/verifyarr_whisper_sync_review_brief.md`
for the original brief). Scoring/analysis scripts:
`/tmp/claude-1000/-home-hammer/037bfcdf-32e4-4d77-b54d-7c7879c6ad97/scratchpad/rev/`.

Not committed to git — working document, kept out of the repo's tracked history until the user
decides what (if anything) here should be committed. **Nothing in this session has been committed
or pushed.** The code changes the recommendations describe are applied to the working tree only.

This document is both the review and the record of what was implemented from it. Where the two
differ — recommendations dropped or changed after later evidence — that is called out inline
rather than quietly edited away, along with the inferences of mine that turned out to be wrong.

---

Method note: I read the production code first and treated the brief as a map, not as evidence. All numbers below are mine, produced this session from the real data. Scripts are in `/tmp/claude-1000/-home-hammer/037bfcdf-32e4-4d77-b54d-7c7879c6ad97/scratchpad/rev/`: analysis in `oracle.py`, `score_lo.py`, `sweep_score.py`, `min_coverage.py`, `sampled_lo.py`, `vad_test.py`, `groq_vs_local.py`, `fit.py`; post-change verification in `e2e_after.py` + `compare_e2e.py`. Baseline before any change: `pytest tests` → **85 passed, 22 subtests passed**.

I built one thing the brief did not have: an **independent word-level oracle** from the Windows `out/*.words.json` (10 ms DTW word times). For every 2-line cue it compares the median reference-word time of each line's own distinctive words. It shares no logic with `line_order.py`, so it can score it.

Two brief corrections: `tests/fixtures/whisper_full/` holds **57** fixtures, not 58 (52 local + 5 Groq). And the S02/S03 validation runs used a **non-default** config — `anchor_check_enabled=True`, `line_order_enabled=True`, `line_order_audio_confirm=True`, `whisper_mode="full"` (`run_s3_slowhorses_validation.py:115-117`). Several headline numbers in §4 are therefore not production-default behaviour.

---

## Headline 1 — The S02 "half came back SUSPECT" finding: the checker was right, the library is broken

§4 posed two hypotheses. It is decisively (a), and more extreme than suspected.

Vocabulary overlap of each on-disk English subtitle against its own episode's reference transcript vs. the *next* episode's:

| episode | overlap with own | with next | verdict |
|---|---|---|---|
| C_S02E14 | 0.292 | 0.897 (E15) | wrong episode |
| C_S02E15 `.en.hi` | 0.226 | 0.936 (E16) | wrong episode |
| C_S02E16 | 0.222 | 0.927 (E17) | wrong episode |
| C_S02E17 | 0.242 | 0.935 (E18) | wrong episode |
| C_S02E20 | 0.284 | 0.920 (E21) | wrong episode |
| C_S02E22 | 0.302 | 0.932 (E23) | wrong episode |
| C_S02E23 | 0.310 | 0.942 (E24) | wrong episode |
| C_S02E02 (control) | 0.939 | — | correct |

**Seven of 24 Community S02 English subtitles are the wrong episode's text** — an off-by-one misassignment. (E15 has both a correct `.en.srt` and a wrong `.en.hi.srt`; the local fixture picked the wrong one, which is why `C_S02E15` scores 0.088 locally and `S02E15` scores 0.920 through Groq — same episode, different file.)

Classifying every "clean"-scenario SUSPECT by what caused it:

- **Content-score SUSPECTs (8):** E08, E14, E15, E16, E17, E20, E22, E23. Seven are proven wrong-episode; E08 is the right text but badly desynced (best word-time agreement 0.10 at any offset/framerate scale within ±90 s). **Zero false positives out of 24 files on the production-default path.**
- **Anchor-escalation SUSPECTs (14):** only reachable with the non-default `anchor_check_enabled=True`. Nine are real (see Headline 2), five are false.

The Δ33-49 s "real sync fixes" that worried §4 are alass confidently fitting a wrong subtitle — the exact failure mode the Whisper stage exists to catch. It caught all of them.

**The E15 architectural edge case is real but is not the tie-breaking logic.** `_resolve_ambiguous_sync` kept `new` at score 0.07 with `blocks` and `old` having no evidence at all — a meaningless choice, correctly flagged SUSPECT afterwards. The actual defect is **ordering**: `_resolve_ambiguous_sync` calls `_write_fix` (`pipeline.py:668/678`) *before* `correctness_and_finish` evaluates `result["flag"]` (`pipeline.py:809`). A file about to be declared SUSPECT has already been overwritten on disk with an arbitrary alass fit chosen on 0.07-quality evidence — and `backup_originals` defaulted to `False`. **Fixed:** when no candidate reaches `overlap_threshold` the original is kept untouched (see P0-2, which also records a remaining gap on the single-block path).

## Headline 2 — The anchor check is better than its "experimental" label; one line made it production-ready

For each anchor-escalated file I measured the *true* local offset independently (word-time agreement in a ±45 s window around each flagged point):

- **True positives (9 files):** C_S03E01 (flags −19.4/−19.7/−19.1 vs measured −19.0), C_S03E02 (+17.25 then −15.0, two real blocks), C_S03E05 (−3.75 then −23.75), C_S03E07 (+22.25), C_S03E11 (tail −18.75), C_S03E14 (+14.25 over the first 6 min), C_S03E20 (−26.75 over most of the file), C_S02E19, C_S02E21. The anchors' reported Δ matches the independently measured local offset to within ~1 s in nearly every case. **alass reported "already in sync" for most of these.**
- **False positives (5 files):** C_S02E01, C_S02E07, C_S03E09, SH_S01E01, SH_S01E06. All show measured local offset ≈ 0.

The separator is not magnitude (a real −3.4 s flag in C_S03E05 was correct) — it is **count**:

| | flagged samples |
|---|---|
| all 5 false positives | 1 or 2 |
| all 9 true positives | 3 to 14 |

**Requiring ≥3 significant anchor residuals gives perfect separation: 9/9 kept, 5/5 suppressed.** Do not require sign agreement — C_S02E19's three flags are +54/+19/−24 and the file is genuinely broken.

Independently confirmed on the coverage sweep across 35 healthy episodes: clean files producing ≥3 significant residuals = **0/35 at every sample setting**, while a +45 s injected shift produces ≥3 in 25/35 at `3 × 60 s`.

## Headline 3 — The shipped version's local full-transcript path produces large repetition loops

Within-episode identical-text runs in the production `small.en-q5_1` fixtures:

| episode | looped cues | share of transcript | span |
|---|---|---|---|
| SH_S01E01 | 336 | 27 % | 556–2083 s |
| C_S02E04 | 185 | 41 % | 104–372 s |
| C_S03E07 | 177 | 30 % | 1014–1252 s |
| C_S02E02 | 122 | 22 % | 372–638 s |
| C_S03E03 | 123 | 19 % | 147–371 s |

The Windows reference (turbo, `-mc 0`) and **every sweep config including `small.en-q5_1`** have essentially none. In C_S02E04 the entire span 103.6–388.9 s (285 s, ~22 % of the episode) is one repeated lyric — all real dialogue there is lost.

**Sampled mode is immune.** A 60 s clip cut at 105 s — the exact loop onset — transcribed with production defaults gives **18 segments, 18 distinct texts, no loop**. The failure needs sustained context across a long single invocation; it does not appear in the clip sizes the default `whisper_mode="sampled"` uses. That is a further argument for sampled as the N100 default, independent of cost.

Measured production damage, same model, same episodes:

| episode | full-mode score | 3×60 score | anchored windows | line-order TP |
|---|---|---|---|---|
| C_S02E04 prod fixture | 0.754 | 0.643 | 16 | 9 |
| C_S02E04 sweep (`-mc 0`) | **0.886** | **0.919** | **36** | **26** |
| C_S02E02 prod fixture | 0.761 | 0.945 | 30 | 16 |
| C_S02E02 sweep (`-mc 0`) | **0.914** | 0.929 | **39** | 19 |

### Attribution — STILL OPEN after five rounds with the Windows peer

I staged the byte-exact audio verifyarr feeds whisper.cpp (`whisper_gpu_staging/verifyarr_audio/C_S02E04.chunk0.exact.mp3` + its piece mapping) so the variables could be separated instead of argued about. Results, all on that same file, same model, same flags:

| run | backend / build | segments | in a run of ≥5 | longest run |
|---|---|---|---|---|
| peer | Vulkan GPU, their build | 184 | 0 | — |
| peer, forced `-ng` | CPU, their build | 210 | 5 (2.4 %) | 3× |
| **this box** | **CPU, unpinned master build** | **325** | **194 (60 %)** | **189×, 109.6–385.6 s** |
| this box `+ -mc 0` | same build | 213 | 0 | 2× |
| this box `+ -mc 0 -nfa` | same build | 194 | 0 | 1× |

**What it is not.** Not the flags (the peer's run with verifyarr's exact flags is clean). Not the audio (same bytes). Not silence trimming — I measured it: it removes 27.2 s = **2.1 %** of C_S02E04, with exactly two cuts anywhere in the 100–390 s loop region. Not the CPU backend as such — the peer's forced-CPU run shows only mild degradation. Not the compiled instruction sets — both builds report identical `AVX2=1 FMA=1 F16C=1 BMI2=1`.

**This affects production. It is not a fixture-build artifact.** That took two wrong turns to establish, both worth recording.

First I concluded it was a version regression — fixtures built from an unpinned master clone (`1da4dc82`) while the Dockerfile pins `v1.9.4`, therefore "fixture defect, not production defect". The peer then checked their own clone: it is *also* `1da4dc82`. Version was not the variable and the conclusion was unsupported.

Then **`v1.9.4` itself was built on the box that reproduces the loop** (tag `v1.9.4`, commit `927cfce`, matching cmake flags) and given the same chunk0 audio and model: **298 segments, one run of 164 identical segments, 109.7–320.7 s** — the same lyric, the same region as the master build's 189-run. **The version the Docker image actually pins is affected**, and real users running `whisper_mode="full"` on the shipped app would hit this. Every hedge about "may well be affected" is now settled the wrong way.

Everything cheap has now been eliminated by direct comparison against the peer's non-looping build:

| variable | mine | peer's | verdict |
|---|---|---|---|
| source commit | `1da4dc82` | `1da4dc82` | identical |
| model file sha256 | `bfdff489…ad30` | `bfdff489…ad30` | **byte-identical** |
| CPU features | `AVX2=1 FMA=1 F16C=1 BMI2=1 OPENMP=1 REPACK=1` | same string | identical |
| cmake | `Release`, `GGML_NATIVE=ON`, `GGML_OPENMP=ON`, `GGML_VULKAN=1` | `Release`, Ninja, Vulkan ON | equivalent |
| threads | `-t 16` → 60 % looped; **`-t 4` (production default) → 55 % looped** | `-t 8` → 2.4 % | not the variable |
| backend | CPU | CPU (forced `-ng`) | not the variable |
| OS / toolchain | WSL Linux, system gcc | Windows MSYS2 UCRT64 gcc 16.2 | **the only difference left** |

**Narrowed to the Linux/gcc CPU backend, by two independent reproductions.** The peer built the same commit on a *fresh, separate* WSL/Ubuntu box (CPU-only Release, gcc 11.4.0) and got the identical failure on the identical audio and model — **164 identical segments, 109.7–320.7 s**, the same run this box produces. They also ruled the audio itself out (sha256, `ffprobe`, a full `ffmpeg` decode, no errors). Their Windows/MSYS2-gcc build on the same inputs produces 5 stray looped segments, not 164.

So: two Linux/gcc builds reproduce it, one Windows/MSYS2 build does not, with source commit, model bytes, flags, threads and audio all held constant. `Dockerfile` builds on `debian:bookworm-slim` with system gcc — the same side — and the N100 target is Linux too.

One caution on how far that goes: what is *established* is the correlation with the Linux/gcc CPU backend across three builds. The **mechanism** — floating-point or scheduling differences changing the decoder's path near a repetition attractor is the obvious candidate — has not actually been demonstrated; nobody has compared logits. Treat "Linux/gcc CPU backend" as the identified trigger and the mechanism as an untested hypothesis.

`-mc 0` was independently confirmed effective on Linux in that fresh-box test (0 repeats with it), which matches this box's own result and settles the decision to ship it: on the Linux target it is mandatory, not a nicety.

`generate._drop_repetition_loops` is therefore a necessary mitigation, not belt-and-braces, and it is deliberately in the app where no build detail can route around it. It logs a warning rather than cleaning up silently, because a triggered loop means the local Whisper build is malfunctioning and the transcript has a real hole in it.

**And `-mc 0` is now shipped after all** — a decision reversed twice as the evidence moved, so here is the whole chain. It was originally proposed on master-build evidence; withdrawn when the peer showed it made a *non*-looping Windows/GPU build slightly worse (0 → 5 looped segments of 213, ~2 %); and reinstated once v1.9.4 — the pinned version — was shown to loop, and then re-run **with** `-mc 0` on the same audio:

| v1.9.4 on the reproducing box | segments | in a run of ≥5 | longest run | 100–180 s |
|---|---|---|---|---|
| as verifyarr called it | 298 | 164 (55 %) | **164×**, 109.7–320.7 s | 5 unique |
| `+ -mc 0` | 213 | **0** | 1× | **33 unique** |

The lyric that had 164 consecutive copies appears exactly once. The guard alone only deletes the damage; `-mc 0` recovers the dialogue, which is what the evidence is made of. Measured on the equivalent pair earlier: anchored windows 16 → 36 and line-order true positives 9 → 26 on C_S02E04. Against losing ~17 % of an episode, a ~2 % short-repeat cost on builds that don't loop is not a close call.

**Two of my own inferences were wrong and are withdrawn.** I told the peer the exactly-1.0 s cadence was my cue splitter manufacturing identical cues; it is in whisper.cpp's raw output, so that concession was wrong — but so was the original inference. The peer's clean runs are 22–33 % exactly-1000 ms and my own clean variants are 6 %, so the metric ranges 6–66 % across healthy and broken runs alike and discriminates nothing; **1.0 s cadence is not evidence of a locked decoder** in either direction. Consequently my "41 % is cue-level, really only ~15 segments" correction was also wrong: raw segment-level is ~189 looped segments against 185 looped cues, essentially 1:1. The original 41 % figure stands.

**Two of my own inferences were wrong and are withdrawn.** First, I conceded to the peer that the exactly-1.0 s cadence was my cue splitter manufacturing identical cues; it is in whisper.cpp's raw output, so that concession was wrong — but so was the original inference. The peer's clean runs are 22–33 % exactly-1000 ms and my own clean variants are 6 %, so the metric is far too variable to diagnose anything. Neither the claim nor the retraction was supported; **1.0 s cadence is not evidence of a locked decoder.** Second, and consequently, my "41 % is cue-level, really only ~15 segments" correction was also wrong: raw segment-level is ~189 looped segments against 185 looped cues, essentially 1:1. The original 41 % figure stands.

Also relevant: `_low_confidence_reason` (`generate.py:594`) is a **dead no-op on the local path** — whisper.cpp `-oj` emits no `no_speech_prob`/`avg_logprob`/`compression_ratio`, and `_parse_local_whisper_json` only produces `{start,end,text}`. The Groq path does get this filtering; local gets none, which is precisely where the loops are.

---

## Answers to the review questions

### 1 & 4 — Architecture: alass-then-Whisper-verify is correct; alass cannot be dropped

**Decisive, single reason:** anchors only exist when the subtitle is in the spoken language (`subtitles.anchors_applicable`). In this very library, **46 of 98 subtitle files (47 %) are Danish on English audio** — zero Whisper timing evidence, ever. alass is language-agnostic and is the only thing that can sync those. Secondary: alass is free, Whisper on an N100 is minutes per file.

They solve different problems and the current split is right: alass answers *where does the timing sit* (blind to content), Whisper answers *is this the right content, and is the timing right at these specific verified instants*. Whisper's timing is good enough to **validate and correct**, not to replace — median |anchor residual| on in-sync files is 0.27–0.37 s, which is confirmation-grade, not better than alass.

The one real opportunity: anchors currently only *choose between* alass's candidates. On files like C_S03E14 (+14.25 s over the first 6 min, alass said "already in sync") the anchors already contain the correct per-region correction. Using them to *correct* rather than only to flag is a genuine future feature — but a separate project, not a reorder.

Keep the order as is.

### 2 — Model and settings, reframed for the N100

**Explicit verdict: large-v3-turbo is NOT necessary. `small.en-q5_1` suffices. `tiny.en` suffices for the correctness/sync decision alone.**

Every sweep config scored through the *production decision logic* (3 episodes, correct subtitle vs. wrong-episode subtitle, segments passed through `split_segments_into_cues` exactly as production does):

| config | clean avg | wrong avg | margin | verdicts | anchored windows | median &#124;shift&#124; | line-order recall |
|---|---|---|---|---|---|---|---|
| large-v3-turbo (reference) | 0.931 | 0.066 | 0.865 | 3/3, 3/3 | 37.0 | 0.27 | 0.628 |
| turbo-q8_0 | 0.932 | 0.069 | 0.864 | 3/3, 3/3 | 38.0 | 0.23 | 0.669 |
| medium.en | 0.927 | 0.071 | 0.856 | 3/3, 3/3 | 36.7 | 0.34 | 0.634 |
| **small.en-q5_1** | **0.915** | **0.068** | **0.847** | **3/3, 3/3** | **37.0** | **0.37** | **0.566** |
| small.en | 0.913 | 0.069 | 0.845 | 3/3, 3/3 | 36.7 | 0.32 | 0.621 |
| base.en | 0.881 | 0.068 | 0.813 | 3/3, 3/3 | 37.0 | 0.35 | 0.586 |
| tiny.en | 0.855 | 0.068 | 0.787 | 3/3, 3/3 | 32.3 | 0.29 | 0.538 |

This table is sweep data (peer's GPU, loop-free). Note that the separate 52-episode fixture corpus every *other* measurement in this review rests on was built on a CPU backend and is loop-contaminated in 9 of 52 episodes — see Headline 3. That makes the fixture-derived small-model numbers a **lower bound**, which only strengthens the verdict: `small.en-q5_1` cleared every bar even on degraded transcripts.

The threshold is 0.25. **Every model from tiny.en up clears it by 0.75–0.87.** This is the "task is more tolerant than raw WER" framing paying off exactly as predicted: the discriminating signal is nowhere near breaking down, even where transcription quality clearly is. Anchors survive too — tiny.en still yields 32 anchored windows at 0.29 s median.

The only metric that separates compute levels is **line-order recall**, and even there turbo→small is ~10 % relative and turbo→tiny ~20 %. `small.en-q5_1` at ~1/3 the memory of `small` is the right N100 default.

- **Beam width — verified, and the brief understates it.** From `tider_gpu.csv`/`tider_cpu.csv`: small.en 56 s → small.en-greedy **31 s (−45 %)**, medium.en 120 s → 61 s (−49 %); on CPU tiny.en 82 → 65 s (−21 %), base.en 134 → 101 s (−25 %). Quality cost at small/base/medium is within ±0.003 on the content score. `_run_local_whisper` passes no beam flags, so it runs whisper.cpp's default 5/5. **Adding `-bs 1 -bo 1` is a 25–45 % saving for no measurable loss.** (tiny.en is the exception — greedy costs it 0.033; another reason not to go below small.)
- **Quantization.** Confirmed on the 5800X3D: q5_1 saved 7–9 % on CPU (tiny 82→75 s, base 134→125 s) — memory, not time. **No N100 data; not asserted either way.** The reason to pick q5_1 on an N100 is the memory footprint against a shared-memory iGPU, not speed.
- **`-ml`/`-sow`.** Measured, not assumed: ran the whole sweep both with and without `split_segments_into_cues`. Line-order recall and anchor accuracy differ by less than the 3-episode noise (reference 0.628 vs 0.586; small.en 0.621 vs 0.641; anchor median 0.27 vs 0.28). Finer segmentation is **worth ≈ 0** here because the cue splitter already approximates it. Skip it.
- **`--vad` on the transcription.** Don't. See item 7.
- **`-dtw`.** **Not worth the rework.** `_judge_order`'s coarse before/after-midpoint split already reaches precision 0.980 (small) / 0.991 (turbo) against the word-level oracle — and the oracle *is* a word-level DTW detector, so that comparison is the fair one. The ceiling `-dtw` would buy is bounded by recall (0.48→0.58), and `-dtw` requires `-nfa`, adding a decode-path change to chase it. There is a **free** recall gain available first — see item 6.
- **Hallucination filter.** `is_nonspeech_annotation` is correct for its job (bracket tags, genuinely up to 79/episode). It catches **0 of the 374** entries in `mistaenkte_hallucinationer.tsv` — but those are ~8/episode against an 0.8 margin, i.e. noise. **A VAD-coverage filter is not the answer** (item 7b). The real local-path problem is the repetition loop (Headline 3).

### 3 & 5 — How little needs transcribing: 90 s for content, 180 s for timing

35 healthy episodes (curated by the independent oracle), sampled mode simulated at `collect_samples`' own dialogue-dense points:

| n × clip | audio | clean → ok | wrong-episode → SUSPECT | clean avg | wrong avg |
|---|---|---|---|---|---|
| 2 × 30 s | 60 s | 35/35 | 25/26 | 0.898 | 0.079 |
| **3 × 30 s** | **90 s** | **35/35** | **26/26** | 0.895 | 0.081 |
| 3 × 60 s | 180 s | 35/35 | 26/26 | 0.904 | 0.095 |
| 12 × 60 s | 720 s | 35/35 | 26/26 | 0.899 | 0.093 |

**90 seconds of audio gives perfect separation, and nothing above it improves anything.** Full mode transcribes ~21 minutes — **14× the compute for zero content-decision gain**. The current default (`sample_count=3`, `clip_seconds=30`) is exactly right for that question.

But the content score **cannot see sync at all** — a +45 s injected shift is flagged by content in **0/35** episodes, because `window_minutes=0.5` makes the comparison window ±30 s wide and a 45 s shift still lands inside it. Sync is answered by alass and by anchors, never by the overlap score.

That is what sets the real floor, and **clip length matters far more than clip count**:

| n × clip | audio | clean FP (≥3 residuals) | +45 s shift detected (≥3) |
|---|---|---|---|
| 3 × 30 s | 90 s | 0/35 | 1/35 |
| 3 × 45 s | 135 s | 0/35 | 14/35 |
| **3 × 60 s** | **180 s** | **0/35** | **25/35** |
| 5 × 30 s | 150 s | 0/35 | 13/35 |
| 5 × 60 s | 300 s | 0/35 | 33/35 |

`ANCHOR_MIN_COUNT = 3` requires three matched lines *within one clip*; a 30 s clip rarely has three, a 60 s clip does. 5 × 30 s uses *less* audio than 3 × 60 s yet detects half as much.

**Recommendation: `clip_seconds` 30 → 60, `sample_count` stays 3.** 180 s of audio, 7× cheaper than full mode, and it is what makes the anchor path work at all.

### 6 — Line-swap detection, scored against an independent oracle

This evaluation did not exist; here it is, over all 52 episodes.

**Full transcript — can it catch and fix every swap?** Catch precisely, yes. Fix every one, no.

| transcript source | TP | FP | precision | recall |
|---|---|---|---|---|
| small.en-q5_1 (production) | 535 | 11 | **0.980** | 0.483 |
| large-v3-turbo (reference) | 642 | 6 | **0.991** | 0.580 |

Precision is excellent; recall is roughly half. The design is correctly biased — it fixes what it is sure of and stays silent otherwise.

The production row understates the detector, because the fixture corpus carries the Headline 3 loop defect. Splitting the 52 episodes by contamination (>5 % of segments inside an identical-text run):

| fixture subset | episodes | TP | FP | precision | recall |
|---|---|---|---|---|---|
| loop-contaminated | 9 | 81 | 3 | 0.964 | 0.367 |
| loop-free | 43 | 454 | 8 | **0.983** | **0.512** |

So fixing the loop is worth roughly +0.15 recall on the affected files, on top of everything else it buys.

**The interleaving trap is real and the bridging guard is holding.** The peer's 39–40 % naive false-positive rate reproduces the risk exactly; the current detector runs at 2 % FP on the same material. `_has_bridging_segment` and the two dynamic discounts are load-bearing — do not weaken them.

But the peer's specific example needs a correction: **Community S02E02's subtitle genuinely has reversed cues.** Cue 11 is `"has never drawn huge crowds," / "Greendale's Oktoberfest"`; cue 16 is `"will be a pop-and-lock-a-thon." / "The central event"`. Cues 4–5 are genuine interleaving as the peer said — this file contains *both* patterns. The "30 auto-fixes on a clean file" in `all_results.jsonl` is not a false-positive epidemic; the oracle independently finds 46 real swaps in that file. The bimodal distribution across the corpus (0–4 fixes vs 30–59) is real: **20 of the 52 episodes have a systematic line-reversal defect, 7 have none.**

**A free recall gain.** `collect_samples_full` selects a cue's segments with `win_lo <= s["start"] < win_hi` (`line_order.py:573`) — by segment *start*. A long bridging segment that begins just before the window is discarded before `_judge_order` ever sees it. Changing to an overlap test (`s["end"] > win_lo and s["start"] < win_hi`):

| source | window test | TP | FP | recall | precision |
|---|---|---|---|---|---|
| small | start | 535 | 11 | 0.483 | 0.980 |
| small | **overlap** | **545** | 11 | **0.492** | 0.980 |
| turbo | start | 642 | 6 | 0.580 | 0.991 |
| turbo | **overlap** | **676** | 6 | **0.611** | 0.991 |

+5.3 % relative recall on turbo, identical false positives, one line.

**Sparse/sampled clips — can it conclude "this episode has a swap"?** Yes, and that is the right framing.

| n × clip | broken files with ≥1 confirmed swap | clean files with ≥1 (false alarm) | per-cue TP | per-cue FP |
|---|---|---|---|---|
| 3 × 30 s | 15/20 (75 %) | **0/7** | 18 | **0** |
| 5 × 60 s | 17/20 (85 %) | **0/7** | 41 | **0** |
| 8 × 60 s | 19/20 (95 %) | **0/7** | 53 | **0** |

Zero per-cue false positives at every setting, and a median of only 1–4 cues confirmed per broken file. So sampled mode is a **reliable screen, not a repair tool** — exactly the "fetch a different release" decision the brief describes. Full mode is what repairs.

### 7 — VAD: usable, but not worth adding

**(a) As a sync signal.** Cross-correlated the independent `.vad.tsv` speech timeline against the subtitle's own cue on/off timeline (50 ms grid, ±60 s):

- In-sync files: recovers the true offset to within **0.25 s** (C_S02E02 +0.25, C_S02E03 +0.25, C_S02E04 +0.15, C_S02E18 0.00, SH_S01E02 0.00).
- C_S03E07: +22.65 s vs. the measured +22.25 s — a real desync found.
- C_S03E02: −14.90 s, correctly matching that file's *second* block (+17.25/−15.0).
- But it fails on C_S03E20 (+0.20 vs −26.75) and reports ≈0 for partial-file problems (C_S03E14, C_S03E11) because a global correlation is right to report ≈0 for a file that is mostly fine.

So: cheap, accurate where it works, **and completely content-blind** — a wrong episode has a similar speech/silence rhythm, so it cannot answer the question the Whisper stage exists for. It would confirm alass, not replace it, and the anchors already give a content-*verified* answer. The token-timestamp bug is not the reason to pass; the reason is that it adds no signal the pipeline lacks. Don't add it.

**(b) As a hallucination filter — actively bad.** Applying a <10 % VAD-coverage rule to the production transcripts:

| episode | loop segs | loop caught | other segs | other wrongly flagged |
|---|---|---|---|---|
| C_S02E02 | 122 | 17 (14 %) | 429 | 102 (24 %) |
| C_S02E04 | 185 | 43 (23 %) | 267 | 69 (26 %) |
| SH_S01E01 | 336 | 214 (64 %) | 886 | 193 (22 %) |

It catches 14–64 % of what you want gone while eating 22–26 % of legitimate dialogue — largely the singing/music case `DATAFORMAT.md` warns about. **Do not build this.** A consecutive-duplicate filter is the right shape instead: collapsing runs of ≥3 identical segments recovers C_S03E07 0.714→0.877 and SH_S01E01 0.660→0.778. But it cannot restore what matters most — C_S02E04's anchors stay at 16 (vs 36) and line-order TPs at 9 (vs 26), because the fabricated timestamps remain. **Preventing the loop beats cleaning up after it.**

### 8 — Settings UI and defaults audit

The UI (`frontend/src/routes/Settings.tsx:530-660`, `699-745`) exposes essentially every relevant knob with accurate tips. Exposure is right; several **defaults** are not.

| setting | was | now | why |
|---|---|---|---|
| `sync.clip_seconds` | 30 | **60** ✅ | Item 3: the single highest-value default change. 1/35 → 25/35 desync detection. |
| `sync.sample_count` | 3 | 3 (unchanged) | 90 s already gives perfect content separation. |
| `sync.overlap_threshold` | 0.25 | 0.25 (unchanged) | Measured clean 0.895 vs wrong 0.081 — the threshold sits in a wide empty gap. |
| `sync.whisper_mode` | `sampled` | `sampled` (unchanged) | Correct for an N100. Full mode buys nothing for correctness; it buys line-order repair and dense anchors. |
| `sync.anchor_check_enabled` | `false` | **`true`** ✅ (the ≥3 rule shipped with it) | With ≥3 it is 9/9 TP, 0/5 FP, 0/35 clean FP. Today it is correctly off — at ≥1 it produces ~10 % false SUSPECTs. |
| `correctness.use_local_whisper` | `false` | `false` (left alone) | `docker-compose.yml` ships `WHISPER_MODEL: small` and the Dockerfile bakes it, yet the setting that uses it is off. Confusing out of the box. |
| `WHISPER_MODEL` (compose/Dockerfile) | `small` | **`small.en-q5_1`** ✅ | ~182 MB vs ~488 MB, equal quality (0.915 vs 0.913), much friendlier to a shared-memory iGPU. `.en` models have no language-detection head, so `_run_local_whisper` now resolves an unknown language to `"en"` for them instead of passing `-l auto` and letting a mistagged foreign track pass the `require_audio_lang` gate on a guess. |
| `general.backup_originals` | `false` | **`true`** ✅ | The pipeline overwrites subtitles in place for sync fixes, line-order swaps, and pre-blacklist. At 0.98 line-order precision, ~2 % of auto-fixes are wrong, with no undo. |
| `/dev/dri` passthrough | commented out | still commented out, comment expanded | `local_whisper_use_gpu` defaults `true`; without the device it silently falls back to CPU. |

Nothing was removed. Two tips were rewritten to match the new behaviour (clip length, anchor escalation).

`use_local_whisper` was left `false` on reflection: turning it on by default would put every install on the local path, and Headline 3 shows that path has an open, unattributed failure mode. The confusing part — an image that bakes a Whisper model but doesn't use it — is a documentation problem, not a reason to flip the switch.

Still **not** added, deliberately: a beam/greedy toggle and a whisper.cpp extra-flags escape hatch. Both amount to exposing decoder internals to the user, and the evidence for the two flags that would have used them (`-bs 1 -bo 1`, `-mc 0`) does not support shipping either.

### 9 — The Groq cloud path holds the same conclusion

Five episodes have both a real Groq `whisper-large-v3` fixture and a local `small.en-q5_1` one:

| episode | source | full-mode clean | 3×60 clean | 3×60 wrong | verdict | anchors | line-order TP |
|---|---|---|---|---|---|---|---|
| C_S02E01 | local | 0.871 | 0.887 | 0.106 | ok | 3 | 31 |
| C_S02E01 | **groq** | 0.909 | 0.905 | 0.104 | ok | 3 | 29 |
| C_S02E06 | local | 0.805 | 0.915 | 0.086 | ok | 3 | 17 |
| C_S02E06 | **groq** | 0.896 | 0.940 | 0.102 | ok | 3 | 26 |
| C_S02E10 | local | 0.852 | 0.890 | 0.139 | ok | 3 | 29 |
| C_S02E10 | **groq** | 0.915 | 0.919 | 0.125 | ok | 3 | 30 |

Same verdicts, same anchor availability, comparable line-order yield; Groq scores 0.03–0.09 higher purely from model size. The 3 × 60 s minimum holds identically. The reason is structural: both paths normalise to the same `{start,end,text}` shape (`_parse_local_whisper_json` / `_transcribe_chunk_openai_compat` → `_normalize_segment`), and no consumer reads DTW or any provider-specific field. **No mode-specific threshold is needed.** The one asymmetry is the one already noted — Groq's verbose_json carries the confidence fields `_low_confidence_reason` needs and whisper.cpp does not.

---

## Prioritized recommendations

**Status: all of P0/P1/P2 below are IMPLEMENTED in the working tree** (uncommitted), except where
the note says otherwise. Two were deliberately dropped after the evidence changed — `-mc 0`
(see Headline 3) and greedy decoding (see below). Verified by a fresh end-to-end run of the real
pipeline over all 52 episodes plus the existing suite — see "Post-change verification" at the end.

**P0 — correctness defects**

1. **"Nothing was verified" no longer reads as a pass.** `_aggregate_correctness` can return `"unknown (no valid samples)"` — every window failed, typically a foreign-language subtitle whose LLM translation never came back — and `correctness_and_finish` branched only on `== "SUSPECT"`, so it fell through to the final `else` and was recorded as `ok`. A local-Whisper user with foreign-language subtitles and no LLM key had every file marked verified having verified nothing. Now its own `unknown` flag, surfaced as a warning pill in Files, as its own filter option, and as "Not verified — no usable Whisper sample" in the activity log. Deliberately does **not** trigger an auto-action: absent evidence is not evidence of a bad file.
2. **Don't write a sync fix to a file you're about to call SUSPECT.** `_resolve_ambiguous_sync` wrote before the verdict was known. Now, when no candidate reaches `overlap_threshold`, the original is kept untouched and the status reads `left unchanged (alass suggested Δ…; no candidate matched the audio's content)`. Covered by a new assertion in `WrongEpisodeTests` (real alass, both directions).

   **Known remaining gap, newly found while verifying this and NOT fixed:** the protection only covers the *deferred* path, i.e. when alass produced a multi-block fit. When alass returns a single global offset, `sync_pair` writes it directly (`pipeline.py`, the `_write_fix` before `correctness_and_finish` ever runs) — no Whisper evidence exists at that point by design. Measured over the 52-episode wrong-episode scenario: **11 of 12 files were correctly left alone, 1 was still re-timed** through this path. Fixing it means deferring *every* sync write until after the content check, reusing the existing `_ambiguous_sync` mechanism; it costs no extra Whisper (the check already runs) but is a real architectural change that needs its own validation pass, so it is written up rather than rushed in here. Mitigated meanwhile by `backup_originals` now defaulting to `true`.
3. **Guard against the repetition loop** — `generate._drop_repetition_loops`. Collapses a run of ≥5 consecutive identical segments to its first instance, and logs a warning rather than cleaning up silently (a triggered loop means the local Whisper build is malfunctioning and the transcript now has a hole in it). Applied in two places: in `full_transcript_for_check`, so it cleans transcripts already cached as well as fresh ones, and at the end of `transcribe_full_track`, so the subtitle-GENERATION feature is covered too — a generated file with the same line 189 times is broken output in its own right, and `_low_confidence_reason` cannot catch it locally (whisper.cpp's `-oj` reports no `compression_ratio`). **`-mc 0` is also shipped** in `_run_local_whisper` — see Headline 3 for why that decision moved twice.

**P1 — free accuracy**

4. `significant_anchor_residuals` now requires **≥3** flagged samples (`ANCHOR_SUSPECT_MIN_SAMPLES`), and `anchor_check_enabled` defaults to `true` on the back of it: 9/9 true positives kept, 5/5 false ones suppressed, 0/35 clean false positives. Covered by five new unit tests. Sampled mode is weaker than full here (3 anchor windows vs ~21), which is exactly why `clip_seconds` went to 60 — at 3 × 60 s it still detects a 45 s shift in 25/35 files with no false alarms.
5. `collect_samples_full`'s clip window: segment **overlap** instead of segment start. +5.3 % relative line-order recall, zero new false positives, one line.
6. ~~Separate consecutive-duplicate collapse~~ — merged into #3, which is the same change; it is the primary mitigation, not belt-and-braces, now that the loop's cause is unattributed.

**P2 — N100 tuning**

7. `clip_seconds` 30 → 60 (`settings.py`, plus the UI tip explaining why). The highest-value default change in the review.
8. ~~Pass `-bs 1 -bo 1`~~ — **dropped, not implemented.** It is 25–45 % faster at ~no cost to the content score, but it does cost ~3 % line-order recall on small.en, and the user's explicit priority is detection precision over compute. Kept as a documented option, not a default.
9. Default model `small.en-q5_1` (`settings.py`, `Dockerfile`, `docker-compose.yml`). `_run_local_whisper` now resolves `language=None` to `"en"` for a `.en` model instead of `"auto"`, so a mistagged non-English track can't pass the `require_audio_lang` gate on a model that has no language-detection head at all.
10. `backup_originals` → `true` (dataclass default, settings-table default, and the UI tip). This surfaced a real robustness bug: `_write_fix` and the line-order path called `backup_subtitle` unguarded, so an unwritable backup dir threw and the fix silently never happened. Now `pipeline._backup_best_effort` warns and continues, matching what `handle_suspect` already did. Also `_run_local_whisper`'s per-clip timeout now scales with `clip_seconds` instead of being a flat 300 s, so raising that setting can't start timing clips out on a slow CPU-only box.

**Leave alone:** the alass-then-verify order; `overlap_threshold=0.25`; `sample_count=3`; `whisper_mode=sampled`; the bridging guard and both dynamic discounts; the minimal 2-entry STOPWORDS list; `is_nonspeech_annotation`; the majority-vote rule in `_aggregate_correctness`; `_reject_unproven_old` and `_confirmed_in_every_block`. All of these are doing measurable work.

**Explicitly do not build:** `-dtw` word-level `_judge_order`; `--vad` on transcription; a VAD-coverage hallucination filter; `-ml`/`-sow`.

---

## Anchor-based resync — correcting the files instead of only flagging them

Added after the review, on the user's instruction. The observation that motivated it: the nine
genuinely mis-timed files all came back SUSPECT **with the correct answer already written in their
own note** — `C_S03E01` "Δ-19.4, -19.7, -19.1", `C_S03E20` "Δ-25.4 … -26.1" — and nothing used it.
`correctness_auto_action` defaults to `off`, and even `blacklist`/`remediate` only asks Bazarr for
a *different release* rather than fixing the file in hand.

An anchor is not just a symptom. `clip_anchor_shift` produces a content-verified measurement of
the real offset at a known instant, so a run of agreeing anchors **is** the correction for that
stretch.

### Design

`subtitles.plan_anchor_resync` / `apply_anchor_resync`, driven from
`pipeline._try_anchor_resync` in the anchor-escalation branch.

1. **Densify.** The planner re-runs the anchor pass at **20 s** instead of 60 s
   (`ANCHOR_RESYNC_INTERVAL_S`). Anchors cost no API call — just token overlap against a
   transcript already in hand — so when the question changes from "is this wrong" to "by how much,
   where", resolution is worth CPU. This is decisive, not cosmetic: at 60 s, `C_S03E05`'s four
   offset changes are backed by 1–2 anchors each and the file must be refused; at 20 s the same
   file resolves into **five regions of 3–13 anchors**.
2. **Segment.** Anchors are grouped into consecutive runs agreeing within `ANCHOR_REGION_TOLERANCE_S`
   (2.5 s — the same number that decides a file is mis-synced at all, so it cannot be worth
   splitting a region over). Grouping compares against the run's running *median*, so one noisy
   reading doesn't split a tight run.
3. **Reject what can't be trusted.** A region needs ≥3 agreeing anchors. This is what refuses a
   file that *drifts* rather than shifts in blocks — measured on `C_S02E19` (+37, +22, +15, +6,
   +2, −1, −3, −12, −31, −39, −36 across the episode): no three consecutive anchors ever agree, so
   no region qualifies and the file is left alone. A thin run **between two runs that agree with
   each other** is absorbed as noise instead (`C_S03E11`'s lone −6.6 s reading); a thin run between
   two *different* offsets is a real transition that can't be placed, and correctly fails the plan.
4. **Cut in the right place.** Region boundaries are put in the widest dialogue gap between the two
   regions — where a real cut sits, and where two differently-shifted halves won't collide. The
   search interval is in **subtitle** time, not audio time; the two differ by exactly that region's
   own shift, and getting this wrong put `C_S03E01`'s cut at 191.6 s instead of ~220 s and left
   two anchors still 19 s out.
5. **Verify before writing.** The plan is applied to a **copy**, fresh dense anchors are computed
   for it, and the file is replaced only if those come back clean. Anchors whose own window
   straddles a cut are excluded from that check — such a window genuinely spans two offsets, so its
   median means nothing (the same discount `_judge_order` applies to a word heard on both sides of
   its split). A correction that doesn't verify is discarded and the file falls back to SUSPECT.
6. **Try the pre-alass original too.** alass runs first and may already have applied a wrong fit,
   which is harder to plan from than the untouched file. Both candidates are tried and the
   verification decides; `sync_pair` keeps the original on the row for this (popped immediately in
   `correctness_and_finish`, never persisted).

Gated by `sync.anchor_resync_enabled` (default on, needs `anchor_check_enabled`). It only ever runs
for a file whose **content already checked out** — re-timing a subtitle that isn't this episode's
text is exactly the mistake P0-2 exists to prevent, and this must not reintroduce it from the other
side.

### Does it actually put the files where the audio says they belong?

Scored with the **independent oracle** (large-v3-turbo word stream, best local offset per twelfth
of the file) — not with the anchors the correction was derived from, which would be marking its own
homework. `rev/resync_check.py`.

| file | regions | worst twelfth before → after | median twelfth before → after |
|---|---|---|---|
| C_S03E01 | 2 | 19.25 s → **0.50 s** | 0.25 → 0.50 |
| C_S03E02 | 2 | 17.75 s → **1.00 s** | 16.62 → **0.50** |
| C_S03E05 | **5** | 23.75 s → 14.75 s | 2.00 → **0.50** |
| C_S03E07 | 2 | 35.25 s → **0.75 s** | 22.25 → **0.38** |
| C_S03E11 | 2 | 18.75 s → **1.25 s** | 0.75 → 0.25 |
| C_S03E14 | 2 | 14.75 s → **1.00 s** | 0.88 → 0.62 |
| C_S03E20 | 4 | 27.00 s → 6.50 s | 22.62 → **0.75** |
| C_S02E21 | 3 | 53.25 s → **2.50 s** | 19.75 → **0.50** |
| C_S02E19 | — | **refused** (continuous drift) | unchanged |

**8 of the 9 corrected, the 9th correctly refused.** Six healthy control files
(C_S02E02/03/18/24, C_S03E13, SH_S01E02) come out byte-for-byte unchanged — the feature does
nothing to a file that doesn't need it.

Two caveats worth stating rather than rounding away. `C_S03E05` and `C_S03E20` still have one
twelfth of the file off by 14.7 s and 6.5 s respectively, even though their medians land at 0.50
and 0.75 s — a short region near a boundary that the block model doesn't capture. And `C_S02E21`
is the file the repo's own `RealWorldFixTests` documents as *not* cleanly auto-correctable; it now
is (53.25 s → 2.50 s), which is a genuine improvement but also means that test's premise is worth
revisiting. Against the user's stated bar — contextually right, not frame-exact — a median of
0.4–0.8 s across a file that was 16–22 s out is comfortably inside it.

The single biggest lever was **using the pre-alass original as a second candidate**. alass runs
first and had already applied a wrong fit to C_S03E02 (Δ25.5 s, 4 blocks), C_S03E07 (Δ53.4 s) and
C_S02E21 (Δ47.1 s); planning on top of that damage fails, planning from the untouched file
succeeds. That took 5 of 9 to 8 of 9. The deferred and direct sync paths keep the original in
different places (`_ambiguous_sync["old_subs"]` vs `_pre_sync_subs`), and missing the first of
those was why the fallback silently did nothing on exactly the files that needed it.

---

## Post-change verification

Not "the change looks right" — the same kind of run the rest of this review is built on.

**1. The repo's own suite.** `pytest tests` → **103 passed, 22 subtests passed** (was 85 + 22).
The 18 new tests cover the repetition guard (5), the anchor minimum-count rule (5) and the
anchor-resync planner (8 -- region detection, outlier absorption, drift refusal, overlap-free
output, negative-timestamp safety), plus a new assertion inside `WrongEpisodeTests`. Note this
suite exercises **sampled mode at the new
`clip_seconds=60`** through its WhisperShim, since `_test_config` deliberately takes
`whisper_mode` and `clip_seconds` from shipped defaults — so the default most installs will
actually run is covered here, not only in simulation.

One failure surfaced along the way and was a real bug, not a test artifact: with
`backup_originals` defaulting on, `_write_fix` called `backup_subtitle` unguarded, so an
unwritable `/data/backups` threw and the sync fix silently never happened. Fixed by
`_backup_best_effort`.

**2. A fresh end-to-end run of the real pipeline** (`rev/e2e_after.py`): `sync_pair` +
`correctness_and_finish`, real alass against the real video files, Whisper served from the
fixtures — 4 scenarios per episode, `whisper_mode="full"`. Compared against this session's
pre-change runs (`all_results.jsonl` + `s3_slowhorses_results.jsonl`), scored against the
independent oracle's verdict on which files are genuinely wrong or genuinely misaligned:

| run | files | flagged correctly | false alarms | missed | precision | recall |
|---|---|---|---|---|---|---|
| before | 52 | 17 | **5** | 0 | 0.773 | 1.000 |
| after | 52 | 17 | **0** | 0 | **1.000** | 1.000 |

All five pre-existing false alarms are gone — `C_S02E01`, `C_S02E07`, `C_S03E09`, `SH_S01E01`,
`SH_S01E06` — and nothing that should have been caught was lost. `SH_S01E01`, previously a false
SUSPECT, now comes back `ok` and gets 3 line-order fixes applied, which a SUSPECT verdict had
been blocking.

Per scenario, after:

- **clean** (n=52): 35 ok, 17 SUSPECT — and all 17 are independently confirmed wrong-episode or
  genuinely misaligned files.
- **global_shift** (+45 s, n=52): same split; alass re-syncs them and the verdict is unchanged,
  which is the correct behaviour — a shifted-but-correct subtitle is a sync problem, not a
  content one.
- **wrong_episode** (n=52): **52/52 flagged SUSPECT**, and the original was left untouched on
  **45/52** — the P0-2 fix. The other 7 go through the single-block path that still writes before
  the content check (the gap recorded under P0-2).
- **line_swap** (n=43): 175/209 injected swaps reverted (84 %, was 70 %). Most of that gain is
  simply that fewer files are blocked by a false SUSPECT verdict. Isolating the detector itself —
  restricting to the **30 episodes that passed correctness in *both* runs** — the overlap-window
  change accounts for the rest: **147/150 (98.0 %) → 150/150 (100 %)**.

---

## What was not settled at review time

- **Loop trigger — RESOLVED; mechanism still unknown.** Narrowed to the Linux/gcc CPU backend and reproduced independently on two separate Linux boxes (this one, and a fresh WSL/Ubuntu box with gcc 11.4.0), against a Windows/MSYS2 build that does not do it — with commit, model bytes, flags, threads and audio all controlled, and the audio itself verified intact. Production's `debian:bookworm-slim` image and the N100 target are both on the affected side. What remains genuinely open is only *why* the two toolchains diverge; that no longer gates any decision, since `-mc 0` (confirmed effective on Linux independently) plus the in-app guard are both shipped.
  - **Being closed as this was written:** `v1.9.4` — the version the Dockerfile pins — is being built on the box that *does* reproduce the loop, with the same cmake flags, and variant A will be run against the same chunk0 audio. That isolates version from toolchain on a single machine and answers "is production's pinned version affected" directly. **Result not yet in; treat the question as open until it is.**
  - If v1.9.4 also loops, the mitigation is `-mc 0` in `_run_local_whisper` — measured to take it from 60 % to 0 % on the affected build — on top of the guard that is already shipped.
  - General rule regardless of outcome: after any `WHISPER_CPP_VERSION` bump, run one long single-invocation transcription (`whisper-cli -m ggml-small.en-q5_1.bin -f <10min audio> -oj -t 4`) and check for a run of ≥5 consecutive identical segments before trusting it.
  - Note the default `whisper_mode="sampled"` is not exposed to this regardless: a 60 s clip at the exact loop onset transcribes cleanly (18 segments, 18 distinct). Only `whisper_mode="full"` is at risk.
- **The 52-episode fixture corpus is contaminated** by this in 9 of 52 episodes. `_drop_repetition_loops` now cleans it on read (verified: −181 segments on C_S02E04, −333 on SH_S01E01, and on a clean episode it removes only genuine 6× and 9× runs), so the fixtures remain usable — but every fixture-derived number here is a lower bound. Rebuilding (~7 h of CPU transcription) was not done. See `tests/fixtures/whisper_full/README.md`.
- **N100 absolute timings.** Every timing above is 5800X3D/RX 6750 XT and the sweep CSVs are contaminated by concurrent runs. Rankings transfer; numbers do not.
- **q5_1 on N100.** No data; flagged rather than asserted.
- **Oracle recall.** The independent oracle judges ~35 % of 2-line cues and abstains on the rest, so the line-order recall figures above are recall *among decidable cases*. Precision is unaffected.
