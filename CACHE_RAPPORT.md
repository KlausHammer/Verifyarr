# CACHE_RAPPORT: model key, atomic WAV, threshold 0.25 s

Date: 2026-09-22. Code under test: HEAD `5e3fc58` + the working tree.
Model: `tiny.en-greedy-cpu`, fresh DB per row, `--workers 14`.
Before = `tests/fix2_final*.jsonl` (HEAD behaviour, 1656/1656 ok).

## 1. The verdict, per change against the right bar

| Change | Arm 1 (fix 100 %) | Arm 2 (detect 100 %) | Healthy files |
|---|---|---|---|
| Findings 1+2 (model key + atomic WAV) | 0 cells of 1656 | 0 cells | 0 |
| Finding 3 (threshold 0.50→0.25) | 96.4 % fixed, 6 silent — **identical, the same 6 cells** | 88.8→**91.2 %** detected (38→30 silent), +8 fully fixed | clean 68/72 identical; genuine 1 new rewrite (evidence-backed, see §5) |

Findings 1+2 are behaviour-neutral as predicted (one model, valid WAVs):
`cache12` vs `fix2_final` gives **0 changed cells**, identical Whisper cost
and identical note hash. Finding 3 moves **112 cells**, all mechanically explained
below. Nothing improves arm 2 at the expense of arm 1. The suite is green
(228 passed, +16 new). **Not committed.**

## 2. Finding 1: the model in the clip cache key

**Design choice: coexistence, not overwriting.** The key is extended from
(video, slot) to (video, slot, STT provider, STT model) — two models' rows
sit side by side per slot. A read under a foreign model is a clean miss,
never a deletion; a write under B does not touch A's row. That meets
both requirements literally (A is never served as B; no delete-and-refetch
loop when switching back and forth) — where the neighbouring table's overwrite pattern
would cost one re-transcription per switch, exactly what the task rules out.
The price is a one-time PK rebuild on migration (SQLite cannot change a PK in
place) and a few more text rows on disk; rows age out after 30 days as before.

- The key comes from the single function `full_transcript_cache_key`, which was moved
  from `correctness.py` to `db.py` so that `vad.py` can key its reads too
  without a circular import (`correctness` re-exports it; all existing
  references work unchanged). The fallback-model caveat is shared with the full cache:
  the key is the *configured* model, not necessarily the one that actually
  transcribed on a fallback.
- The NULL convention is followed: rows without a key (stored before the columns existed)
  are accepted under any model; when they share a slot with a keyed row, the
  keyed one wins. An mtime/size mismatch still deletes ALL of the video's rows (the audio is
  gone — applies to all models).
- Callers that now carry the key: `correctness_check` (read+write),
  `evaluate_against_cached_transcripts` (anchors!), `collect_samples` (3 reads,
  1 write), `timeline_for_video`. Stale "keyed on" comments in
  `db.py`/`correctness.py`/`pipeline.py`/`e2e_matrix.py` have been updated.

**Migration (requirement 2):** `test_old_db_migrates_without_losing_rows` builds an
old-schema DB (frozen HEAD DDL + 2 rows), opens it with `connect()` and
shows that both rows survive with their content, are readable through the new API and can
sit next to new keyed writes. The rebuild is a no-op on fresh DBs
(PK check via PRAGMA, explicit column list since ALTER adds new columns last).

**Tests/mutation:** 6 db tests, all green after, all 6 red with the fix
stashed (together with the 10 other new ones: 16/16 red without the fix).

## 3. Finding 2: atomic `extract_audio_wav`

ffmpeg now writes to `<name>.part` alongside, and only after
exit 0 **and** a passed `wav_complete` (RIFF/WAVE magic + declared size =
actual file size) is the file moved into place with `os.replace`. A failure deletes
the tmp file and never touches a previously good file (`-y` used to truncate it).
The harness (`audio_cache_for`) now rejects an invalid staging WAV with the same
validator instead of seeding it blindly — all 60 staging WAVs validate,
so the measurements are untouched by that change. As the task said: no live failure found
here; the gaps were real, but unmet.

**Tests/mutation:** 8 tests (4 validator units + 4 mocked ffmpeg runs:
failure/timeout/truncated-on-exit-0/success), all red without the fix — the three
behaviour-deciding ones fail on assertion (partial file left behind / truncated WAV
accepted), not on TypeError. `test_seed_points_at_existing_wav` now writes
a minimal valid WAV instead of `b"fake-wav"` (the expectation is unchanged).

## 4. Finding 3: threshold 0.50→0.25 s, in both places

`SCREEN_TOLERANCE_S` and the `sync.min_change_seconds` default are both 0.25 with
updated comments; `test_screen_and_write_gate_agree` pins that the two
places agree. **Upgrade semantics (as the task asks):** the default
only reaches installs without a stored value (`get_all_settings`: a stored row
wins). An upgrader who has ever saved sync settings **stays at
0.5 in the write gate**, while the screen (a code constant) moves for everyone — that is
a 0.25 screen + 0.5 gate: more alass runs, the same write limit. Fresh
installs get 0.25/0.25. The matrix below measured 0.25/0.25 (a fresh DB
follows the default; `cfg_for` does not pin it). No migration of stored 0.5 values
was made — that is the user's decision.

**112 changed cells** (`cache_all` vs `cache12`), 16 verdict changes + 96
path changes with the same verdict:

- **8× warned→fixed, all cut_version full (SH):** rec 0.44-0.51→0.97-1.00 via
  alass 2-block. The 0.5 screen had cleared them ("already in sync" on a file with a
  300 s jump); 0.25 sends them to alass. Pure gains, still flagged SUSPECT.
- **8× silent→warned:** C_S02E12 cut_version/piecewise_c sampled (rec ~unchanged,
  now honestly flagged) plus C_S02E02/C_S03E10 cut_version sampled, where the
  newly written single-offset fix is **worse than nothing** (rec 0.58→0.05 and
  0.56→0.00) — but flagged. The mechanism is an exposed, pre-existing
  weakness in the verify path (it writes a SUSPECT-scored candidate: "kept 'new'
  [score 0.10 (SUSPECT)..."), not threshold logic as such. Out of scope;
  noted here so the decision is informed.
- **34× uniform_p03 already→fixed (Δ0.3-0.4):** the intended boundary shift.
  6 were already fixed at 0.5; 32 still stand untouched (measured < 0.25 — the noise floor).
- **14× uniform_m07:** 6 new real fixes (2× Δ0.7; 4× Δ0.5 on C_S03E16 —
  residual error ~0.2 s), 8× Δ wobble (1.0/1.1→0.9), rec 1.0 everywhere.
- **10+6+4+4 × uniform/p15/m5/neg:** Δ wobble of 0.1 s (rec 1.0). uniform_neg
  full (the 6.1 cells!) now arrives via the verify path instead of presync (spread
  0.21/0.25 ≥ 0.25 lifts the veto) — the same file result, a different route.
- **4× dropdup already→fixed (Δ0.3 s, rec 1.0):** a noise-floor write on
  timing-sound-but-damage
...[truncated 2336 chars]
