# Tests

`python -m pytest tests` runs on a clean checkout and on GitHub Actions. Nothing copyrighted is in the repo.

## Two kinds of test

**Self-contained** (run everywhere, including GitHub): unit tests, tests on small inline or synthetic subtitles
and the stored transcripts in `fixtures/whisper_full/`, the API, settings, Docker entrypoint and so on.

**External-data tests** (skipped when the data is missing, reported as `skipped` with the reason): they run
the real pipeline and real alass on real episodes. The media, the subtitles and the stored Whisper transcripts
of those episodes are copyrighted, so they stay on your own machine. The test code is in the repo; you point it
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

The full matrix (`e2e_matrix.py`, 23 scenarios) needs the same data.
