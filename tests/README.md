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

## What runs, and on what

`python -m pytest tests` runs everything from a clean checkout in about a minute, with no media, no network and no folder
outside the repository. There are no skipped tests apart from two that cannot make sense in a given environment (the
permission tests when run as root, the timezone test without `time.tzset`).

- **Unit tests** use small inline subtitles and synthetic anchors: settings, Docker entrypoint, API, anchor and resync
  maths, line order, the CPU core settings and so on.
- **Pipeline tests** run the real pipeline (screen, alass step, correctness check, resync, undo net) on the Known Good
  episodes. The stored Whisper output and the recorded alass answers stand in for audio and for alass, so they are
  deterministic and need nothing else. They derive from `kg_env.KgReplayCase`, which installs the dataset hooks for one test
  class and removes them afterwards. The files are `test_clean_files` (a verified-correct subtitle is never rewritten or
  flagged), `test_silent_rows`, `test_swap_gate`, `test_fps_guard`, `test_fps_rescale`, `test_drift_ramp`,
  `test_screen_order`, `test_fix_61_62`, `test_remediate_keep` and `test_known_good_dataset` (a small matrix from the packaged data).

A pipeline test that needs an alass answer that has not been recorded stops with `alass replay miss`. A maintainer with the
media records it once and merges it into the repository:

```bash
VERIFYARR_KG=auto python -m pytest tests/test_x.py     # runs the real alass and records the answer
python tests/known_good/replay.py merge
```

Older tests ran on Community, Slow Horses S01E02-E06 and Z5 episodes from a local folder. They were ported to the Known Good
episodes (the cases, not the numbers: a docstring that quotes a figure may come from the older episode) or removed where
no Known Good episode shows the same thing.

The full matrix (`e2e_matrix.py`, 58 scenarios) runs from the packaged Known Good data with `VERIFYARR_KG=replay`
(see [known_good/README.md](known_good/README.md)).
