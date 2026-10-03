# Tests

`python -m pytest tests` runs on a clean checkout and on GitHub Actions.

## The reference set: `tests/known_good/`

Every number the README and the docs quote about detection, repair and model choice comes from ten episodes the
owner has verified as correct: **[tests/known_good/](known_good/README.md)**. The folder holds the original subtitles, the
stored Whisper output of 14 local configurations and Groq, the VAD intervals, every alass answer the matrix needs and the
results, so the whole measurement can be run again on any machine. No video and no audio is included.

> **Copyright.** The subtitles and the Whisper transcripts in `tests/known_good/data/` (and the older material in
> `tests/data/`, `tests/fixtures/` and `tests/arkiv_*/`) are subtitle and speech text of commercial TV episodes. They are
> published here only as test evidence for this tool, for research and reproducibility, and belong to their rights holders. If you are a
> rights holder and want something removed, open an issue and it will go.

Older research data (Community, the earlier Slow Horses set, the Z5 and Z100 library runs, `tests/data/`, `tests/arkiv_*/`)
is kept for history. No quoted number depends on it.

## Two kinds of test

**Self-contained** (run everywhere, including GitHub): unit tests, tests on small inline or synthetic subtitles
and the stored transcripts in `fixtures/whisper_full/`, the API, settings, Docker entrypoint and so on.

**External-data tests** (skipped when the data is missing, reported as `skipped` with the reason): they run
the real pipeline and real alass on real episodes. The media of those episodes is copyrighted, so it stays on your own machine. The test code is in the repo; you point it
at your own copy of the data:

```
export VERIFYARR_TEST_DATA=/path/to/data
python -m pytest tests -ra
```

Layout under `VERIFYARR_TEST_DATA`:

| Path | Content |
|---|---|
| `whisper_gpu_staging/sweep/<model>/<slug>.json` | stored whisper.cpp output per episode and model |
| `whisper_gpu_staging/wav/<slug>.wav` | 16 kHz mono audio of the episode (optional; ffmpeg extracts it otherwise) |
| `Season 2/`, `Season 3/`, `Slow Horse/Season 1/`, `Known Good/` | the video and its subtitle |
| `Z5_flaggede/<slug>/` + `meta.json` | the five real flawed episodes |

`VERIFYARR_VAD_BINARY` optionally points at `whisper-vad-speech-segments` (the matrix uses VAD when it exists).

The full matrix (`e2e_matrix.py`, 58 scenarios) runs from the packaged Known Good data with `VERIFYARR_KG=replay`
(see [known_good/README.md](known_good/README.md)); against your own media it needs the layout above.
