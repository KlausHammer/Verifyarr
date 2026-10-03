# Known Good: the reference set

Every number Verifyarr quotes about how well it works comes from these ten episodes. The owner has watched each of them
and confirmed that the subtitle is correct and in sync, so the original subtitle is the truth: any change the pipeline
makes to an unmodified file is a false positive, and any error injected into one is known exactly.

| Slug | Episode | Subtitle |
|---|---|---|
| KG_BB_S01E01 | Breaking Bad S01E01 | `.en.hi.srt` |
| KG_BIL_S01E01 | Billions S01E01 | `.en.srt` |
| KG_BMS_S01E01 | Blue Mountain State S01E01 | `.en.hi.srt` |
| KG_BOB_S15E01 | Bob's Burgers S15E01 | `.en.hi.srt` |
| KG_EUP_S01E01 | Euphoria (US) S01E01 | `.en.hi.srt` |
| KG_PB_S05E01 | Peaky Blinders S05E01 | `.en.hi.srt` |
| KG_SHAM_S05E06 | Shameless (US) S05E06 | `.en.srt` |
| KG_BOYS_S01E01 | The Boys S01E01 | `.en.hi.srt` |
| SH_S01E01 | Slow Horses S01E01 | `.en.srt` |
| SH_S01E06 | Slow Horses S01E06 | `.en.srt` |

One thing is not verified: whether a few lines inside a cue are in the wrong order. The matrix reports how many lines the
swap heuristic flags on the untouched files (`clean_swap_candidates` in `results/summary.json`), which bounds it.

## What is in the repo (and what is not)

No video and no audio. Everything the pipeline needs to be run again is stored:

| Path | Content |
|---|---|
| `data/episodes.json` | slug, series, video name, subtitle source, duration, language |
| `data/subtitles/<slug>.srt` | the original, verified subtitle |
| `data/transcripts/<model>/<slug>.json.gz` | stored whisper.cpp output (segments with times) for 14 local configurations and Groq `whisper-large-v3-turbo` |
| `data/vad/<slug>.vad.tsv` | Silero VAD speech intervals |
| `data/alass/<slug>.jsonl.xz` | every alass answer the matrix needs, recorded once with the real alass and replayed |
| `data/speed/` | CPU transcription speed per model, measured on five-minute excerpts with four threads |
| `results/matrix.jsonl.xz` | every matrix row (10 episodes x 15 models x 58 scenarios x 2 modes) |
| `results/summary.json`, `results/tables.md` | pass rates per error type, model and mode |
| `results/model_quality.json`, `results/model_quality.md` | word accuracy and anchor quality per model |

alass fits a subtitle to the audio, and the audio is not in the repo. The matrix therefore replays recorded alass
answers: the key is the episode, the exact subtitle that went in, and the alass flags. alass is deterministic, so a replay
gives the same file the real run gave. Whisper is replaced the same way: the stored transcripts are what `whisper.cpp`
wrote for the full episode, and the sampled mode slices its short clips out of them.

## Run it

```bash
pip install -r requirements.txt
VERIFYARR_KG=replay python tests/e2e_matrix_parallel.py --workers 8 \
    --scenarios "$(cat tests/known_good/scenarios.txt)" --mode full,sampled --audio-confirm off --out kg_run
python tests/known_good/analyze.py tests/kg_run.jsonl --out /tmp/kg_result
python tests/known_good/model_quality.py --out /tmp/kg_result
```

One model on one episode, to try something quickly:

```bash
VERIFYARR_KG=replay python tests/e2e_matrix.py --only KG_BOB_S15E01 --models tiny.en-greedy-cpu \
    --scenarios clean,uniform,drift,missing_middle --mode full,sampled --audio-confirm off --out kg_try
```

Use `VERIFYARR_KG=replay` to prove a change against the stored answers. If your change makes the pipeline hand alass a
different subtitle than before, the replay stops with `alass replay miss`. Then rerun those rows with
`VERIFYARR_KG=auto`: it runs the real alass when it has the audio (see below) and records the answer.

## Rebuild the data (maintainers, needs the media)

```bash
VERIFYARR_TEST_DATA="/path/to/folder" python tests/known_good/build_dataset.py    # subtitles, transcripts, VAD
VERIFYARR_KG=auto python tests/e2e_matrix_parallel.py ...                           # records missing alass answers
python tests/known_good/replay.py merge                                             # one xz file per episode
```

`VERIFYARR_TEST_DATA` points at a folder with `Known Good/` (video + subtitle), `whisper_gpu_staging/wav/<slug>.wav`,
and the whisper sweeps under `whisper_gpu_staging/{sweep,sweep_linux,kg_sweep_gpu}/<model>/<slug>.json`.
`groq_transcribe.py` makes the Groq transcripts through the app's own transcriber.

## The scenarios

58 scenarios, defined in `tests/e2e_matrix.py` and listed in `scenarios.txt`. Each corrupts the verified subtitle in one way
(seeded, so every run injects the same error), then runs the pipeline and checks what it did against the original.

| Group | Scenarios | Pass means |
|---|---|---|
| Constant offset | +45 s, -45 s, -0.7 s, +1.5 s, -5 s (and +0.3 s, reported on its own) | fixed: median error <= 0.25 s and 98 % of lines within 1 s |
| Rate errors | framerate 23.976/24 both ways, PAL 24/25 both ways, 2 % drift, drift + 8 s offset, 10 random drift/ratio draws | fixed, same bar |
| Blocks at different offsets | 3 piecewise, cut version, 16 random block/cut-step draws | flagged (repair is a bonus) |
| Missing stretch | 5-minute hole, 4 random holes | flagged and left untouched; counted when >= 20 dialogue lines went missing |
| Start or end cut off | 4 random cuts | flagged; the first and last two minutes are not judged on purpose |
| Wrong episode | another episode's subtitle | flagged SUSPECT and left untouched |
| Swapped lines | swapped line pairs, many swaps | flagged for review |
| Drift + swapped lines | both at once | timing fixed and swaps flagged |
| Dropped/duplicated cues | 5 % dropped, 5 % duplicated | left untouched |
| Per-line jitter | +-1..3 s on every cue | no worse than injected |
| Healthy file | no corruption | left untouched and not flagged |

`full` mode hands the pipeline the whole transcript. `sampled` is what the app uses with models bigger than tiny: short clips placed by the real
code, cut from the same transcript, with escalation to the whole transcript when the clips disagree.
