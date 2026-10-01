# Song lyrics as sync evidence (task F)

**Conclusion: don't build it.** Song-lyric anchors ARE worse than average
(10.3 % wrong against a base rate of 3.9 %) — but the contribution is vanishingly small
(13 of 289 wrong anchors, 4.5 %), concentrated in two musical numbers, and all
filters tried discard data without moving the estimate (median/MAD and
clip level identical). On top of that come two findings that undermine the fix itself: in HI,
song↔song anchors are **flawless (30/30)** — genuine, usable evidence that an
unselective filter would destroy; and the transcriber family production
uses (large-v3: turbo 0/13,749 ♪, groq 0 ♪ in 5 fixtures) does not mark
song with ♪ at all, so a ♪ rule would be a no-op right there — while unmarked song lyrics
(the Christmas carols below) slip through any ♪ rule. A negative result
with numbers behind it — the details follow. Raw data: `sangtekst.json`.

Measured on the same 30 ground-truth episodes and with the same matching as task E
(`_match_segments_to_lines` replica: ±10 s candidates, ≥0.5 overlap, ≥2 shared,
strictly `>` so the earliest line wins, one anchor per line, offset 0,
correct = |residual| ≤ 2.5 s) — but on the fixture transcription (local
small.en), because it is the **only** one that marks song with ♪/♫. Absolute
anchor numbers are therefore not 1:1 with E (7,470 vs. 7,352 anchors, error rate 3.9 %
against E's 3.4–3.6 %) — the comparison that counts is song against non-song and
the filter delta within the same transcription. Everything is split by HI/non-HI:
17 HI episodes (243 ♪ cues, 2.98 %) against 13 non-HI (17 ♪ cues, 0.24 %).

## 1. How much song lyric is there?

- Transcription side: **304/17,615 segments (1.73 %)**, **885/37,015 s
  (2.39 %)** with ♪/♫. Of them 31 without content tokens (the `♪ La la la ♪` type) —
  they can never anchor. The current `_NONSPEECH_RE` catches 1 of 304.
  By comparison: bracket music without ♪ (the `[Music]` type, already filtered
  in production) accounts for 123 segments.
- Concentration: **C_S03E03 alone accounts for 149 of the 304 song segments
  (49 %)** — one musical number (the series theme, The 88's, diegetic 139–162 s
  plus reruns). Without that episode: 155 segments (0.91 %).
- Subtitle side: HI carries the counterpart (243 ♪ cues), non-HI almost does not
  (17 cues) — but "almost" is not "never": C_S03E03's `.en.srt` itself contains the
  theme lyrics (italic + a few ♪ cues).
- Anchors today from a ♪ line on one side or the other: **126/7,470
  (1.69 %)** — 41 segment side (0.55 %), 101 line side (1.35 %), 16 both.
  Only 41 of 273 content-bearing song segments (15 %) become anchors.

## 2. Are song anchors worse? (HI/non-HI)

| Group | All (n, errors) | HI (n, errors) | Non-HI (n, errors) |
|---|---|---|---|
| base | 7,470, 3.9 % | 4,016, 3.3 % | 3,454, 4.5 % |
| seg ♪ | 41, **9.8 %** | 30, **0.0 %** | 11, **36.4 %** |
| line ♪ | 101, **9.9 %** | 96, 9.4 % | 5, 20.0 % |
| both ♪ | 16, 6.2 % | 12, 0.0 % | 4, 25.0 % |
| either ♪ | 126, **10.3 %** | 114, 7.9 % | 12, 33.3 % |
| no ♪ | 7,344, 3.8 % | 3,902, 3.2 % | 3,442, 4.4 % |

The same pattern in the turbo transcription (E data, unmarked song):
line-♪ anchors 100/7,352 (1.36 %) with 6.0 % errors — HI 94 @ 6.4 %,
non-HI 6 @ 0 %. Two independent transcriptions, the same direction.

The mechanism is mapped, not merely counted: **all 4 wrong non-HI
seg anchors** are C_S03E03's theme segments, whose timestamps small.en
puts 3–9 s wrong (−3.1…−9.0 s residual on a genuine lyric match).
**All 9 wrong HI line anchors** are Christmas carols in C_S03E10 (+ one
theme line in C_S02E18), where small.en transcribes the stanzas **without ♪** as
ordinary dialogue — the content matches the right lyric line, but the
segment timing is 2.5–7.2 s off. Two points: (a) the whole measured
excess risk sits in two musical numbers — without C_S03E03 the non-HI seg side is
0/7 wrong; (b) a segment-side ♪ rule catches **none** of the 9
carol errors, because the segments are not marked — only the line side would.

Song-related errors are 13/289 (4.5 %) of all wrong anchors.

## 3. The repeated chorus — refuted as a distinct danger

Distant doppelgängers: 399/7,470 (**5.3 %**, E: 5.5 %) with a 5.3 % error rate
(E: 8.9 % — another transcription). Of them song: **16/399 (4.0 %), all in HI,
zero in non-HI**. Residual distribution:

- doppelgänger+song (n=16): errors 6.2 %, |resid| median 0.40 s, p90 2.01 s,
  max 7.18 s.
- doppelgänger+non-song (n=383): errors 5.2 %, median 0.43 s, p90 1.52 s,
  max 8.39 s.

No systematic difference; the large residuals exist in both groups, and
the max is larger outside song. The large song residuals that ARE observed
(C_S03E03 −3…−9 s, the carols −2.5…−7.2 s) are **mis-timed genuine matches**, not
a chorus matched to the wrong occurrence — the earliest-line-on-a-tie mechanism
gives no measurable extra tail here.

## 4. Music OVER speech — cannot be delimited, and there is nothing to gain

- Proposal 1 (a VAD passage with both ♪ and dialogue): **14 candidates across 30
  episodes → 8 anchors, 0 wrong.** The population almost does not exist,
  because Silero VAD fires inconsistently on song (C_S02E01/SH_S01E05: 0 % of the
  song time in VAD; C_S03E03: 53 %). By construction it cannot carry a
  filter — and a side note to E: song confounds VAD absence as a
  background sign the other way.
- Proposal 2 (proximity ±15 s around song, 391 candidates → 202 anchors):
  error rate **2.0 % against 3.9 % in the base rate** (HI 2.9 %/3.3 %; non-HI
  **0/65 against 4.5 %**). Near-song dialogue is no worse — if anything better.
- A confidence dip without a missing subtitle: **does not exist.** Word confidence
  (out/ words) median 0.908 in proximity against 0.912 overall; and the
  low-confidence→wrong AUC **collapses 0.63 → 0.35** inside the proximity
  (HI 0.66 → 0.37; non-HI: no wrong ones in proximity at all).
  Even E's weak signal (AUC ~0.70) disappears — inverted — near music.
  Three independent noes.

## 5. What would a change cost and give? (the same yardstick as E)

| Variant | Anchors (lost, of which wrong) | Error rate | Median / MAD / std | Clips conf. / med\|shift\| |
|---|---|---|---|---|
| V0 baseline | 7,470 | 3.87 % | −0.104 / 0.387 / 1.137 | 89/90, 0.194 s |
| V1 drop all ♪ segments | 7,429 (41 / 4) | 3.84 % | −0.104 / 0.386 / 1.130 | 89/90, 0.195 s |
| V2 drop ♪ seg only non-HI | 7,459 (11 / 4) | 3.82 % | −0.104 / 0.387 / 1.129 | 89/90, 0.194 s |
| V3 drop ♪ both sides | 7,344 (126 / 13) | 3.77 % | −0.102 / 0.385 / 1.126 | 89/90, 0.195 s |

V2 has the best precision (4 wrong of 11 lost) and keeps HI's flawless
song↔song evidence — but moves **nothing**: the same median, the same MAD, the same
89/90 confident clips. V3 also catches the carol errors (13 wrong of 126
lost) for the same zero gain. By E's yardstick all three are **a loss of
data**. Build neither V1 (destroys 30/30 correct HI evidence) nor V3
(126 anchors for 13 errors, no estimate gain); nor V2 — 0.05
percentage points on the error rate without a clip effect is not worth a change.

## Caveats

- Only in-sync files: the interplay with a real sync error is not measured.
- The ♪ population is defined by small.en's marking; a ±10 s window against
  production's minute-wide clips (full-window doppelgängers are measured separately,
  §3). Italic-without-♪ lyric lines are not flagged — the line side's ♪ figure is a
  lower bound.
- C_S03E03's 149 segments dominate the song mass; without it the non-HI
  seg side is flawless (0/7).
- Q4's confidence uses out/ word times laid over fixture segments (the same
  audio, a different segmentation) — the direction (no dip, collapsed AUC) is robust
  to that, absolute levels less so.
- The Groq finding (0 ♪, 0 brackets in 5 fixtures) applies to the fixture sample; whether
  the sweep run marks differently has not been investigated (`sweep/` untouched per
  the rules).

## Reproduction

Analysis (scratch, not part of the deliverable): `/tmp/song_analyse.py`
(per-episode JSON) + `/tmp/song_eval.py` (Q1–Q5) + `/tmp/song_json.py`
(deliverable JSON), run with the project's `.venv` python (pysubs2). Read:
`verifyarr/subtitles.py` (`tokenize`, `is_nonspeech_annotation`,
`_match_segments_to_lines`/`_robust_clip_shift` as a contract),
fixtures, media subtitles, `out/*.vad.tsv` + `out/*.words.json` as
auxiliary timelines, and `/tmp/bg_all30.json` for the turbo supplement.
Nothing committed, `out/`/`sweep/`/`wav/` untouched.
