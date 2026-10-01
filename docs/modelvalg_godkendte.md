# Model choice on approved episodes (2026-09-30)

Approved episodes only: Slow Horses S01E01–E06 (SH) + Known Good (KG): Billions, Blue Mountain State, Bob's Burgers, Breaking Bad, Euphoria. No Community. 14 local models + Groq whisper-large-v3-turbo (cloud). Code: HEAD d85517e (+ `groq-turbo` added in `tests/e2e_matrix.py`). Matrix: 11 episodes × 23 scenarios × full+sampled × audio-confirm off = 506 rows per model. Raw data: `matrixdata/godkendte_2026-09-30/`.

## Names
- **local** = runs on your own machine (whisper.cpp): tiny/base/small/medium, and local large-v3-turbo in compressed form (`turbo-q5_0`, `turbo-q8_0`).
- **cloud** = a service over the network: here only **Groq large-v3-turbo** (`groq-turbo`).
- The old `turbo` in the matrix is NOT turbo: for Slow Horses the fixture files are local small.en-q5_1. It is left out of the comparison.

## Table (sampled = production / full)
Passed/total over all scenarios except `uniform_p03` (+0.3 s lies just above the 0.25 bar: boundary noise, 4–9 of 11 passed for all models). Criteria as in `modelvalg_detektion.md`.

| model | sampled SH | sampled KG | full SH | full KG | word F1 SH / KG | speed (× real time, CPU 4 threads, alone) | RAM |
|---|---|---|---|---|---|---|---|
| **tiny.en-greedy (prod)** | 131/132 | 108/110 | 130/132 | 108/110 | 0.747 / 0.814 | 25 | 0.6 GB |
| tiny.en | 128/132 | 108/110 | 128/132 | 108/110 | 0.758 / 0.820 | 17 | 0.6 GB |
| base.en-greedy | 131/132 | 108/110 | 130/132 | 109/110 | 0.811 / 0.857 | 16 | 0.7 GB |
| small.en-greedy | 131/132 | 109/110 | 130/132 | 109/110 | 0.872 / 0.894 | 5.8 | 1.2 GB |
| small.en | 131/132 | 106/110 | 130/132 | 109/110 | 0.871 / 0.892 | 4.5 | 1.3 GB |
| medium.en-greedy | 131/132 | 106/110 | 130/132 | 108/110 | 0.893 / 0.913 | 2.1 | 2.4 GB |
| medium.en | 130/132 | 107/110 | 131/132 | 107/110 | 0.886 / 0.902 | 1.8 | 2.7 GB |
| local large-v3-turbo q8_0 | 131/132 | 107/110 | 130/132 | 109/110 | 0.881 / 0.888 | 1.7 | 1.8 GB |
| local large-v3-turbo q5_0 | 131/132 | 107/110 | 129/132 | 109/110 | 0.887 / 0.889 | 1.3 | 1.5 GB |
| **cloud Groq large-v3-turbo** | 123/132 | 109/110 | 124/132 | 109/110 | 0.884 / 0.892 | approx. 50 (55–78 s per episode, network) | – |

(All 14 local models and all scenarios in `analyse.txt` / `per_model_all.txt`; medium.en-q5_0 is missing SH_S01E03 — DTW crash.)

## What the numbers say
1. **Detection/fixes: no difference between the local models.** Differences are 1–3 rows out of about 240 and there is no ranking. Requirements (offset/rate 100 %, blocks/missing_middle 100 % detected): blocks 44/44 and missing_middle detected for all local models; the few failures are `jitter` (identical for all models — alass chases the noise) and the boundary cell `uniform_p03`. Tiny is no worse than small/medium on KG (other series, thinner dialogue): 108/110 against 106–109/110. Medium.en-greedy fails three rows on Bob's Burgers (uniform_m07, fps_late, missing_middle) that tiny does not — a single episode, noise, not decided.
2. **Word accuracy: tiny is clearly the worst.** F1 0.75/0.81 against 0.87/0.89 (small) and 0.89/0.91 (medium). Fewer anchors (SH 54 against 67–72 per 10 min), but not worse anchors (share ≤0.5 s: 0.65–0.72 for all). It does not show up in the detection (point 1).
3. **Cloud (Groq turbo) is no better at detection, and slightly worse on SH:** 123/132 against 131/132. 7 of 9 failures are SH_S01E05 rate scenarios where the residual after the fix is 0.254–0.268 s against the bar of 0.25 s (marginal — everything fixed, residual error slightly above the bar); plus jitter (all models) and two single rows. Probable cause: cloud gives only segment times (no DTW word times), anchors ≤0.5 s are lowest (0.62 SH). Word F1 0.884/0.892 = like local medium/turbo. Note: cloud transcripts also have repetition loops (4–28 segments dropped per episode in 6 of 11 episodes) — it is not only a tiny trait.
4. **Speed (CPU alone, 4 threads):** tiny.en-greedy approx. 25× real time, small 4.5–5.8×, medium 1.8–2.1×, local turbo 1.3–1.7×. A 58 min episode: tiny approx. 2 min, small approx. 12 min, medium/turbo approx. 30–45 min. Times are from 5 min excerpts (startup counts, which makes tiny/base slightly pessimistic; a full episode with tiny is 30×), and run with nice 19 without core 0. Full times per episode: `docker_test/kg_sweep/godkendte_tider*.csv`.

## Conclusion (facts, not a recommendation)
On the approved episodes the model choice does **not** change anything about what the app detects or fixes — tiny is as good as small/medium/local turbo, and cloud turbo is no better (marginally worse timing on one series). The model choice is between **speed/resources** (tiny: 25× real time, 0.6 GB) and **better words** (small/medium: +12–14 F1 points, 5–14× slower) — the word difference is not used by the detection. Limitation: 11 episodes, one row = noise; thin dialogue is covered only by the KG episodes (no Community, they have swapped lines). The decision is the user's (prod = tiny.en + VAD, 26/9).
