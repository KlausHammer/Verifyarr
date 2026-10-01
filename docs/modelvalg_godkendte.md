# Modelvalg på godkendte afsnit (2026-09-30)

Kun godkendte afsnit: Slow Horses S01E01–E06 (SH) + Known Good (KG): Billions, Blue Mountain State, Bob's Burgers, Breaking Bad, Euphoria. Ingen Community. 14 lokale modeller + Groq whisper-large-v3-turbo (cloud). Kode: HEAD d85517e (+ `groq-turbo` tilføjet i `tests/e2e_matrix.py`). Matrix: 11 afsnit × 23 scenarier × full+sampled × audio-confirm off = 506 rækker pr. model. Rådata: `matrixdata/godkendte_2026-09-30/`.

## Navne
- **lokal** = kører på egen maskine (whisper.cpp): tiny/base/small/medium, og lokal large-v3-turbo i komprimeret form (`turbo-q5_0`, `turbo-q8_0`).
- **cloud** = tjeneste over nettet: her kun **Groq large-v3-turbo** (`groq-turbo`).
- Den gamle `turbo` i matrixen er IKKE turbo: fixture-filerne er for Slow Horses lokal small.en-q5_1. Den er udeladt fra sammenligningen.

## Skema (sampled = produktion / full)
Bestået/ialt over alle scenarier undtagen `uniform_p03` (+0,3 s ligger lige over 0,25-baren: grænsestøj, 4–9 af 11 bestået for alle modeller). Kriterier som i `modelvalg_detektion.md`.

| model | sampled SH | sampled KG | full SH | full KG | ord-F1 SH / KG | hast. (× realtid, CPU 4 tråde, alene) | RAM |
|---|---|---|---|---|---|---|---|
| **tiny.en-greedy (prod)** | 131/132 | 108/110 | 130/132 | 108/110 | 0,747 / 0,814 | 25 | 0,6 GB |
| tiny.en | 128/132 | 108/110 | 128/132 | 108/110 | 0,758 / 0,820 | 17 | 0,6 GB |
| base.en-greedy | 131/132 | 108/110 | 130/132 | 109/110 | 0,811 / 0,857 | 16 | 0,7 GB |
| small.en-greedy | 131/132 | 109/110 | 130/132 | 109/110 | 0,872 / 0,894 | 5,8 | 1,2 GB |
| small.en | 131/132 | 106/110 | 130/132 | 109/110 | 0,871 / 0,892 | 4,5 | 1,3 GB |
| medium.en-greedy | 131/132 | 106/110 | 130/132 | 108/110 | 0,893 / 0,913 | 2,1 | 2,4 GB |
| medium.en | 130/132 | 107/110 | 131/132 | 107/110 | 0,886 / 0,902 | 1,8 | 2,7 GB |
| lokal large-v3-turbo q8_0 | 131/132 | 107/110 | 130/132 | 109/110 | 0,881 / 0,888 | 1,7 | 1,8 GB |
| lokal large-v3-turbo q5_0 | 131/132 | 107/110 | 129/132 | 109/110 | 0,887 / 0,889 | 1,3 | 1,5 GB |
| **cloud Groq large-v3-turbo** | 123/132 | 109/110 | 124/132 | 109/110 | 0,884 / 0,892 | ca. 50 (55–78 s pr. afsnit, netværk) | – |

(Alle 14 lokale modeller og alle scenarier i `analyse.txt` / `per_model_all.txt`; medium.en-q5_0 mangler SH_S01E03 — DTW-crash.)

## Hvad tallene siger
1. **Detektion/rettelser: ingen forskel mellem de lokale modeller.** Forskelle er 1–3 rækker af ca. 240 og ingen rangorden. Krav (offset/rate 100 %, blokke/missing_middle 100 % detekteret): blokke 44/44 og missing_middle detekteret for alle lokale modeller; de få fejl er `jitter` (identisk for alle modeller — alass jager støjen) og grænsecellen `uniform_p03`. Tiny er ikke dårligere end small/medium på KG (andre serier, tyndere dialog): 108/110 mod 106–109/110. Medium.en-greedy fejler tre rækker på Bob's Burgers (uniform_m07, fps_late, missing_middle) som tiny ikke gør — enkeltafsnit, støj, ikke afgjort.
2. **Ord-nøjagtighed: tiny er klart dårligst.** F1 0,75/0,81 mod 0,87/0,89 (small) og 0,89/0,91 (medium). Færre ankre (SH 54 mod 67–72 pr. 10 min), men ikke dårligere ankre (andel ≤0,5 s: 0,65–0,72 for alle). Det slår ikke igennem i detektionen (pkt. 1).
3. **Cloud (Groq turbo) er ikke bedre til detektionen, og lidt dårligere på SH:** 123/132 mod 131/132. 7 af 9 fejl er SH_S01E05 rate-scenarier hvor resten efter rettelsen er 0,254–0,268 s mod baren 0,25 s (marginalt — alt rettet, restfejl lidt over baren); dertil jitter (alle modeller) og to enkeltrækker. Sandsynlig årsag: cloud giver kun segment-tider (ingen DTW ord-tider), ankre ≤0,5 s er lavest (0,62 SH). Ord-F1 0,884/0,892 = som lokal medium/turbo. Bemærk: cloud-transskripter har også gentagelsesløkker (4–28 segmenter droppet pr. afsnit i 6 af 11 afsnit) — det er ikke kun en tiny-egenskab.
4. **Hastighed (CPU alene, 4 tråde):** tiny.en-greedy ca. 25× realtid, small 4,5–5,8×, medium 1,8–2,1×, lokal turbo 1,3–1,7×. Et 58 min afsnit: tiny ca. 2 min, small ca. 12 min, medium/turbo ca. 30–45 min. Tider er fra 5 min uddrag (opstart tæller, gør tiny/base lidt pessimistisk; fuldt afsnit tiny 30×), og køres med nice 19 uden core 0. Fuld-tider pr. afsnit: `docker_test/kg_sweep/godkendte_tider*.csv`.

## Konklusion (fakta, ikke anbefaling)
På de godkendte afsnit ændrer modelvalget **ikke** noget for hvad appen opdager eller retter — tiny er lige så god som small/medium/lokal turbo, og cloud-turbo er ikke bedre (marginalt dårligere timing på én serie). Modelvalget står mellem **hastighed/ressourcer** (tiny: 25× realtid, 0,6 GB) og **bedre ord** (small/medium: +12–14 F1-point, 5–14× langsommere) — ord-forskellen bruges ikke af detektionen. Begrænsning: 11 afsnit, én række = støj; tynd dialog er kun dækket af KG-afsnittene (ingen Community, de har byttede linjer). Beslutningen ligger hos brugeren (prod = tiny.en + VAD, 26/9).
