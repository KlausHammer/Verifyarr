# Findings

Everything that was found, measured and decided while building and testing Verifyarr, in one place. Most notes are written in
Danish as they were made. **These are working notes, not documentation.** The numbers the README quotes come from the
Known Good set (`tests/known_good/`); several notes below are about older data (Community, Slow Horses, the Z5 and Z100 library
runs) and are kept as the record of what was found and why a rule looks the way it does.

| File | What it is |
|---|---|
| [fund_og_fejl.md](fund_og_fejl.md) | **The master log**, written as it happened: bugs, disproved assumptions, tests, decisions. Newest entries at the bottom (Known Good facit, code review, Z100 rerun, the user's verification of rewritten files) |
| [z100_genkoersel_2026-10-04.md](z100_genkoersel_2026-10-04.md) | Z100 library episodes rerun on the newest code: before and after for each of 41 episodes, with season and episode |
| [afsluttende_review.md](afsluttende_review.md), [detection_review.md](detection_review.md) | Two independent code reviews of the detection and repair paths |
| [missing_middle_rapport.md](missing_middle_rapport.md), [swap_og_old_rapport.md](swap_og_old_rapport.md), [jitter_review_svar.md](jitter_review_svar.md), [jitter_recheck_svar.md](jitter_recheck_svar.md) | Measurements behind the missing-stretch detector, the swap gate / "old wins" rule and the jitter rule |
| [cache_rapport.md](cache_rapport.md), [fix_rapport.md](fix_rapport.md) | Model-key / atomic-WAV / 0.25 s threshold work, and findings 6.1/6.2 measured to completion |
| [modelvalg_detektion.md](modelvalg_detektion.md) | Detection per model (older 11-episode set; the current model comparison is in [docs/models_and_tests.md](../docs/models_and_tests.md)) |
| [plan_simplify_rester.md](plan_simplify_rester.md) | The simplification findings that were and were not applied |
| [archive/](archive/) | Older reports replaced by the Known Good ones, kept for history |

Not in the repository on purpose: the rewritten Z100 subtitles with their originals (copyrighted text, kept locally for checking) and
raw run output.
