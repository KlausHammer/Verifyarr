# Test data

Everything the tests need to run, without media files. Raw audio and video are not
here — see "What is not included" below.

## Folders

| folder | content |
|---|---|
| `sweep/<model>/<episode>.json` | model transcriptions, 14 model configs × 10 episodes |
| `reference/` | the large-v3-turbo reference: `.words.json` (word times + confidence), `.vad.tsv` (speech/silence), `.srt`, `.lang.json` |
| `subtitles/` | the original subtitle files the tests corrupt and measure against |
| `rapporter/` | measurement reports: which episodes qualify as ground truth, background speech, song lyrics, clip granularity |
| `raw/sweep/<episode>.tar.xz` | **full** whisper.cpp JSON, all models for that episode: tokens, confidence per token, DTW times |
| `raw/reference.tar.xz` | **full** large-v3-turbo JSON with tokens for all 10 episodes |

## The 10 episodes
Nine are confirmed by the user **and** measurement-verified as suitable ground truth:
C_S03E03, C_S03E08, C_S03E10, SH_S01E01–E06.

C_S03E04 is included as a documented drift case: confirmed correct content,
but −0.048 s/min of genuine drift. It does not score recovery against its own timings (see
`DRIFT_CASE_SLUGS` in `e2e_matrix.py`) — it measures whether the drift is *detected*.

The selection is documented in `rapporter/afsnit_tillid.md`: 30 of 52 episodes qualify
as ground truth, and the machine "good episodes" list did not hold.

## Format of the sweep files
Whisper.cpp's raw output is cut down to what the tests use — 241 MB became 7 MB:

```json
{"model": "small.en-q5_1", "slug": "C_S03E03", "language": "en",
 "segments": [{"start": 4.99, "end": 6.59, "text": "..."}]}
```

Times are in **seconds** (raw whisper.cpp mixes milliseconds in `offsets` and
centiseconds in `t_dtw` — see `DATAFORMAT.md` in the staging folder).

Token-level data (confidence per token, DTW times) is not in the normalised
version — it is 97 % of the bulk and is not used by the tests. But it is **preserved**
in `raw/`, compressed:

```
tar -xJf tests/data/raw/sweep/C_S03E03.tar.xz      # 14 models, raw JSON
tar -xJf tests/data/raw/reference.tar.xz           # turbo with tokens
```

241 MB raw becomes 14.7 MB. The files are packed **per episode**, because the 14 models
transcribe the same audio and therefore resemble each other — the compression reuses that
(16x). One big archive with everything gave only 13x, since xz's dictionary does not reach across 241 MB.
Extraction is verified byte-identical to the source.

The anchor studies (background speech, song lyrics) used exactly token confidence and
DTW times — that is what made it possible to reject word-level anchors with numbers.

Note: `base.en-greedy-cpu/SH_S01E05` had an invalid UTF-8 byte in whisper.cpp's
output and is read with `errors="replace"`. One character in one segment text.

## What is not included
- **media files (.mkv)** — 55 GB
- **wav** — 3.2 GB, 16 kHz mono, lives in `whisper_gpu_staging/wav/`
- **Whisper models** — 6.4 GB
- **raw `out/*.json`** — 17 MB for these 10 episodes; `.words.json` is the distillate

All of it lives on the Windows machine under
`C:\Users\knham\Desktop\undertekst auto\`. The tests point at `wav/` for alass'
audio reference — if it does not exist, audio is extracted from the video instead (slow,
but it works).
