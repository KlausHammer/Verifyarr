# Full-episode Whisper transcripts

Five real, full-episode Whisper transcriptions (Community S02E01/E06/E10/E15/E21), each
`{slug}.json` containing the whole episode's `{"start","end","text"}` segments at absolute
video timestamps, built once via `python3 tests/build_whisper_fixtures.py` (real Groq API calls,
~3 ten-minute chunks per episode) and reused free from then on.

- `tests/sync_verification.py` builds dense, independent ground truth from these (matching every
  subtitle line against the whole episode, not just a handful of clips) and serves them back
  through a shim that stands in for the app's real STT calls, so `tests/test_sync_verification.py`
  can run the actual sync/correctness/line-order pipeline end-to-end, at zero ongoing API cost.
- Regenerate a fixture with `--force` if generate.py's chunking/silence logic changes in a way
  that would meaningfully change its output; otherwise these don't need touching.

S02E15 and S02E21's *original* subtitle files (as downloaded when these fixtures were first
built) each had their first 14-16 cues collapsed to `00:00:00,000`, with otherwise normal timing
for the rest of the file -- see `KnownEdgeCaseTests` in `test_sync_verification.py` for the
mechanism that most plausibly explains it (pysubs2 clamping negative timestamps to zero on
save, consistent with an earlier over-correcting auto-sync attempt on those two files). Both
have since been replaced on disk with clean subtitles and their `subtitle_name` fields updated
to match (the transcripts themselves needed no rebuild -- same audio, only the subtitle
changed):

- **S02E15** is now a healthy baseline (9% ground-truth coverage, ~0.2s median residual) and is
  in `HEALTHY_SLUGS` alongside S02E01/S02E06/S02E10.
- **S02E21**'s replacement is internally consistent for most of the episode (24% coverage,
  ~0.3s median residual) but has a REAL, substantial pre-existing drift of its own in roughly
  its back third -- not a fixture defect, a genuine problem in that file. It is deliberately
  NOT in `HEALTHY_SLUGS` (injecting a synthetic break on top of an already-broken baseline
  confounds what's being tested -- see the original audit's S02E03 finding for the same
  confound) and is instead used AS-IS in `RealWorldFixTests`, which also documents two real
  bugs `_resolve_ambiguous_sync` had that this file's genuine complexity (alass fits it as
  FIVE blocks) surfaced and that running this file through the suite led to fixing.

## The 52 local fixtures (`C_S02*`, `C_S03*`, `SH_S01*`) -- two known caveats

Built with local whisper.cpp (`ggml-small.en-q5_1.bin`) rather than Groq, via
`generate.full_transcript_for_check`. Two things to know before trusting a number derived
from them:

1. **Repetition loops in 9 of the 52.** The whisper.cpp build used locks the decoder into
   repeating one line for minutes at a time: C_S02E04 has a single 189-segment run covering
   110-386s, and up to 41% of a transcript is affected (SH_S01E01, C_S02E04, C_S03E07,
   C_S02E02, C_S03E03 are the worst). Root cause is NOT established -- source commit, model
   file (sha256-identical), decoder flags, thread count, audio, backend and compiled CPU
   features were all matched against a build that does not do it, leaving only the OS/compiler
   toolchain. Do not assume the shipped Docker image is exempt; it has not been tested.
   `generate._drop_repetition_loops` strips these on read, so the fixtures are usable as-is --
   but any per-segment count taken straight from the JSON is inflated, and line-order recall
   measured on the affected episodes runs ~0.15 lower than on the clean ones.
2. **Seven of the Community S02 subtitles are the WRONG EPISODE's text** -- not a fixture bug,
   a real defect in the library they were built from, confirmed against an independent
   large-v3-turbo reference (vocabulary overlap 0.22-0.31 with their own episode, 0.89-0.94
   with the NEXT one): C_S02E14, E15's `.en.hi.srt`, E16, E17, E20, E22, E23. These are
   genuinely useful as wrong-subtitle test cases, but they are NOT clean baselines and must not
   be treated as such. (E15 also has a correct `.en.srt` on disk; the fixture points at the
   `.en.hi.srt`, which is E16's text.)
