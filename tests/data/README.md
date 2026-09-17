# Testdata

Alt hvad testene behøver for at køre, uden mediefiler. Rå lyd og video ligger
ikke her — se "Hvad der ikke er med" nedenfor.

## Mapper

| mappe | indhold |
|---|---|
| `sweep/<model>/<afsnit>.json` | modeltransskriptioner, 14 modelconfigs × 10 afsnit |
| `reference/` | large-v3-turbo-referencen: `.words.json` (ordtider + konfidens), `.vad.tsv` (tale/stilhed), `.srt`, `.lang.json` |
| `subtitles/` | de originale undertekstfiler testene korrumperer og måler mod |
| `rapporter/` | målerapporter: hvilke afsnit der duer som facit, baggrundstale, sangtekst, klipgranularitet |
| `raw/sweep/<afsnit>.tar.xz` | **fuld** whisper.cpp-JSON, alle modeller for det afsnit: tokens, konfidens pr. token, DTW-tider |
| `raw/reference.tar.xz` | **fuld** large-v3-turbo-JSON med tokens for alle 10 afsnit |

## De 10 afsnit
Ni er bekræftet af brugeren **og** måleverificeret som facit-egnede:
C_S03E03, C_S03E08, C_S03E10, SH_S01E01–E06.

C_S03E04 er med som et dokumenteret drift-tilfælde: bekræftet korrekt indhold,
men −0,048 s/min ægte drift. Den scorer ikke recovery mod sine egne timings (se
`DRIFT_CASE_SLUGS` i `e2e_matrix.py`) — den måler om driften bliver *opdaget*.

Udvælgelsen er dokumenteret i `rapporter/afsnit_tillid.md`: 30 af 52 afsnit duer
som facit, og den maskinelle "gode afsnit"-liste holdt ikke.

## Format på sweep-filerne
Whisper.cpp' rå output er skåret ned til det testene bruger — 241 MB blev 7 MB:

```json
{"model": "small.en-q5_1", "slug": "C_S03E03", "language": "en",
 "segments": [{"start": 4.99, "end": 6.59, "text": "..."}]}
```

Tider er **sekunder** (rå whisper.cpp blander millisekunder i `offsets` og
centisekunder i `t_dtw` — se `DATAFORMAT.md` i staging-mappen).

Token-niveau data (konfidens pr. token, DTW-tider) er ikke i den normaliserede
udgave — det er 97 % af fylden og bruges ikke af testene. Men det er **bevaret**
i `raw/`, komprimeret:

```
tar -xJf tests/data/raw/sweep/C_S03E03.tar.xz      # 14 modeller, rå JSON
tar -xJf tests/data/raw/reference.tar.xz           # turbo med tokens
```

241 MB rå bliver 14,7 MB. Filerne er pakket **pr. afsnit**, fordi de 14 modeller
transskriberer samme lyd og derfor ligner hinanden — komprimeringen genbruger det
(16x). Ét stort arkiv med alt gav kun 13x, da xz' ordbog ikke rækker over 241 MB.
Udpakning er verificeret byte-identisk med kilden.

Anchor-undersøgelserne (baggrundstale, sangtekst) brugte netop token-konfidens og
DTW-tider — det var dét der gjorde det muligt at afvise ordniveau-anchors med tal.

Bemærk: `base.en-greedy-cpu/SH_S01E05` havde en ugyldig UTF-8-byte i whisper.cpp'
output og er læst med `errors="replace"`. Ét tegn i én segmenttekst.

## Hvad der ikke er med
- **mediefiler (.mkv)** — 55 GB
- **wav** — 3,2 GB, 16 kHz mono, ligger i `whisper_gpu_staging/wav/`
- **whisper-modeller** — 6,4 GB
- **rå `out/*.json`** — 17 MB for disse 10 afsnit; `.words.json` er destillatet

Alt det ligger på Windows-maskinen under
`C:\Users\knham\Desktop\undertekst auto\`. Testene peger på `wav/` for alass'
lydreference — findes den ikke, udtrækkes lyd fra videoen i stedet (langsomt,
men virker).
