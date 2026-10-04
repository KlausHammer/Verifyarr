# FIX_RAPPORT: findings 6.1/6.2 measured to completion

Date: 2026-09-22. Code under test: HEAD `2d44f6d` + the working tree's
`verifyarr/pipeline.py`. Model: `tiny.en-greedy-cpu`. All numbers are measured through the
pipeline (`run_one`), never offline on the side.

## 1. The verdict

The fix works, but the short-circuit that came with it (`if single_block: return
True`) was **removed again after ablation**: it bound 10 new silent rows and
32 unintended cell changes in total, no intended ones. Without it the
fix changes exactly the 18 intended cells of 1656 — 6× presync write (6.1)
and 12× untouched wrong content (6.2) — and nothing else. Whisper cost
identical before/after (N100 measured, not assumed). Genuine files: 8→2
wrong-content rewrites (not 0 — see §5). The suite is green.

## 2. Requirement 1: mutation test (measured, not assumed)

```
git stash push verifyarr/pipeline.py
.venv/bin/python -m pytest tests/test_fix_61_62.py -q   # 2 failed (4.4s)
git stash pop
.venv/bin/python -m pytest tests/test_fix_61_62.py -q   # 2 passed (3.1s)
```

On HEAD both fail as predicted: C_S03E10 uniform_neg reports "already in
sync" on a file that is 45 s off; C_S02E01 wrong_episode writes `fixed (Δ44.4s)`
on wrong content. After the fix (also after the short-circuit removal,
rerun): `fixed (Δ44.8s, presync)`, rec 1.0 and `left unchanged
(alass suggested Δ44.4s; no candidate matched...)` + SUSPECT respectively. Both tests
validate the fix — no rewrite needed.

## 3. Requirements 2+3: before/after matrix, cell by cell

`matrixdata/fix_before_*.jsonl` could NOT be reused: it covers only 4 of 8
holdout episodes, SH partially (57–81 of 92 rows/cell); only 4 known
Community episodes are complete. Both arms were therefore rerun from scratch, full sample:
18 episodes × 23 scenarios × 2 modes × 2 audio = 1656 rows/arm, `--fresh-db
--redo --workers 14`. Before = `/tmp/vbase` (`git archive HEAD`, verified
byte-identical to `git show HEAD:verifyarr/pipeline.py`); after =
the working tree. Both arms 1656/1656 ok. (A `.venv` copy was unnecessary: the launchers
use the working tree's venv; only the `verifyarr/` import switches via
`VERIFYARR_UNDER_TEST`.)

First after-measurement (WITH the short-circuit): **50 changed cells** — 32 too many.
All are explained below; they are the reason for the removal in §4.

Final after-measurement (without it, `fix2_final` vs `fix2_before`): **exactly 18
changed cells, 0 unexplained**:

| Cells | Change | Mechanism |
|---|---|---|
| uniform_neg full, C_S02E01/C_S02E12/C_S03E10 × on/off (6) | silent→fixed | the 6.1 main branch: correct presync (+45 s) + rejected alass 2-block. The baseline is now written as `fixed (Δ.., presync)`, rec 0.000→1.000, instead of "already in sync" on the wrong original on disk |
| wrong_episode, C_S02E01/C_S02E04/C_S02E12 × full/sampled × on/off (12) | fixed→untouched, SUSPECT kept | the 6.2 branch: single-block deferral, no content matches → `left unchanged`, file untouched. The flagging (100 %) was there before; the contract now holds |

No other class changes on any of the 18 episodes. `fix2_final` vs
`fix2_abl` (the ablation run): **0 changed cells** — proof that
the `presync_desc` removal (§7) is behaviour-neutral end to end.

### The 32 cells the short-circuit bound (the reason for removal)

All are single-block deferrals where "old" won without anchor coverage:

- **6× fixed→silent** (C_S02E04/C_S02E12 fps_early sampled, C_S02E04
  uniform_p15 sampled; rec 1.0→0.84/0.0, flag ok): the content score is blind to
  small shifts (window overlap), so a noise gap > the 0.1 margin let old win outright,
  bypassing the anchor comparison. Measured on uniform_p15: old 0.8468 vs new ~0.75
  (bar 0.7468 — decided by ~0.001), while the anchors stood at 0.7 s (new) against
  2.2 s (old) over 13 clips each and were never consulted.
- **4× warned→silent** (C_S02E01 jitter × full/sampled × on/off):
  alass' Δ10.1s fit on jitter is junk (rec 0.0 in both arms — the rejection is
  correct), but the flag was lost: old is evaluated ok and does not escalate.
- **6× rec worse, still flagged** (C_S02E02 piecewise_b/c 0.485/0.18→0.0,
  C_S03E16 piecewise 0.037→0.0): partial alass fixes rejected.
- **2× silent→warned** (C_S03E09 piecewise_c, rec 0.425→0.010, flag
  ok→SUSPECT): honest flagging instead of a silent partial fix — the only
  improvement the line bound, at the price of a recovery collapse.
- **14× neutral path changes** (rec 1.0 in both): presync/framerate/anchor routes
  instead of alass direct (among others C_S02E12 fps_late via the framerate branch and
  uniform_m5 via anchor resync after an old win — the safety net works, a different
  route to the same fix).

## 4. The suspect line (ablation)

Variant `/tmp/vabl` = the fix minus exactly the 3 lines, full 1656-row
matrix: **18 changed cells against HEAD — only the intended ones** (verdict changes:
6 silent→fixed, 12 n/a→n/a). So the line binds 0 intended and 32
unintended cells, 10 of them new silent ones. **Removed.** One comment line
remains, justifying the absence with the measurement. The `single_block` field and
variable are kept (note branches + `apply_pending_sync` use them).

Known tradeoff (analytical, not measured — genuine was not run with the line):
without the line, old can only win a single block through no content match or
absence of anchors; that costs C_S02E19 untouched on genuine files (see §5). With the line
it was untouched, but the price was 10 silent matrix rows. Silent is the worst
class — the removal stands.

## 5. Requirement 4: genuine files

`.venv/bin/python /home/hammer/overfit/genuine.py` (104 rows, the working tree's
code) against `radata/genuine.jsonl`:

| Class | Before (rewritten / SUSPECT) | After |
|---|---|---|
| wrong content (18) | 8 / 18 | **2** / 18 |
| fit as ground truth (30×2) | 1 / 0 | 1 / 0 (the same SH_S01E04-full FP, **0 new**) |
| right content out of sync (26) | 17 / 9 | 17 / 9 (identical) |

6 rows rescued (C_S02E14/C_S02E16 Δ1.1s, C_S02E17 Δ49.6s — all now `left
unchanged` + SUSPECT). The expectation "8→0" did not hold: **C_S02E19 full+sampled
are still rewritten (Δ39.6s), both flagged SUSPECT.** Mechanism: old scores
highest on content (full 0.60 vs 0.48; sampled 0.68 vs 0.30-SUSPECT) but is
rejected without anchor coverage (empty ranges make `_confirmed_in_every_block`
unsatisfiable), so new is written; the subsequent anchor escalation flags it.
Detection holds, the untouched contract still breaks here — an honest negative
result, no regression (the row was rewritten before too). Output saved as
`tests/fix2_genuine_after.jsonl` (the script's own copy lives in /tmp and may
disappear).

## 6. Requirement 5: Whisper cost (N100 measured)

Aggregated over the whole matrix, before vs after (final):

- `fresh_audio_s`: 400049.1 s in both arms — **0 new Whisper calls**
- `cached_audio_s`: 3393278.2 s in both arms — not even cache rereads moved
- Cells with a changed `whisper_cost`: **0 of 1656**

"The check already runs" is now a measurement: single-block deferral scores 2
candidates on already-paid samples and costs exactly 0.

## 7. The points of doubt (HANDOFF §4, all settled)

1. `max_shift_new` binding: bound via `.get()` in the function head (l.978) before
   any use; the whole suite + 3×1656 matrix rows executed without error. Closed.
2. `_synthetic("old")` after a presync write: describes the disk. Proof: the branch is
   reached only with winner="old" ∈ content_ok, so `scored["old"]` is the baseline's
   own evaluation, and the baseline is exactly what is written. Closed.
3. The below-threshold proxy: **stands as an approximation** — 0 matrix cells took the
   `had_presync` branch (measured via the note signature), so it is uncovered by the
   matrix; the error is upper-bounded by the threshold (0.5 s). Honestly open.
4. The `sync_pair` early return moves correctness sampling on presync: measured
   neutral — no diff cell can be attributed to it (all presync rows outside
   the 18 are cell-identical). Closed.
5. The short-circuit: removed after ablation (§4). Closed.
6. New status strings: all consumers use `startswith("fixed")`
   (`db.py`, `jobs.py`, `reports.py`, frontend `StatusPill.tsx` → pill ok);
   the "left unchanged" variant falls into the existing muted class as before.
   No strict parsers. Final rows never contain
   "[pending verification]" (deferral always resolves before persist). Closed.
7. Empty `blocks_time_ranges`: forces the content branch by construction
   (`_confirmed_in_every_block` returns False on empty ranges →
   content branch). Shown end to end: the wrong_episode note scores both
   candidates (new 0.06, old 0.07). Closed.
8. `presync_desc`: **removed** (a dead value — the note already carried the description
   via `presync_note`). Neutrality proven: final vs ablation 0 cells. Closed.

## 8. Artifacts and status

- Final diff: `verifyarr/pipeline.py` (the fix minus the short-circuit minus
  `presync_desc`), `tests/test_fix_61_62.py` (untouched, both pass).
- Raw data (untracked, in the repo): `tests/fix2_before*.jsonl`,
  `tests/fix2_final*.jsonl`, `tests/fix2_abl*.jsonl`,
  `tests/fix2_genuine_after.jsonl`. `/tmp/vbase` (clean HEAD) and `/tmp/vabl`
  (ablation) still exist, but /tmp may disappear — the jsonl files are
  the proof.
- Suite: **212 passed, 44 subtests, 0 skipped** (4:25 min).
- **Not committed** — ready for Claude's review.
- What did not succeed: genuine 8→2 instead of 8→0 (C_S02E19, both
  flagged); the below-threshold proxy is unmeasured on matrix data (0 cells).
