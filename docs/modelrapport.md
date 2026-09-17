# Hvor godt virker whisper-modellerne til sync og detection?

Målt på 52 afsnit (Community S02+S03, Slow Horses S01) med large-v3-turbo som
facit, 14 billigere konfigurationer på 3 afsnit, sparse-sampling på alle 52
samt 4 danske Stormester-afsnit. Maskinlæsbar version: `modelrapport_ai.json`.

**Metoden kort:** hvert undertekstord slås op i whisper-ordtider; `aftale` er
andelen der findes inden for ±2 s. Bedste offset søges i ±60 s (trin 0,5 s).
Alt nedenfor er målt — se **Hullerne** til sidst for hvad der mangler.

## Model mod model (aftale på E02/E03/E04 + sek./afsnit)

Tiderne er rangering, ikke benchmarks (GPU- og CPU-arbejderen kørte samtidig).

| Model | Aftale | Tid |
|---|---|---|
| turbo (reference) | 0,91–0,94 | — |
| turbo-q8_0 | 0,88–0,94 | 43 s |
| medium.en-greedy | 0,88–0,94 | ~60 s |
| small.en-greedy | 0,87–0,93 | ~30 s |
| small.en / q5_1 | 0,86–0,93 | ~55 s |
| base.en (±greedy) | 0,84–0,90 | ~100 s |
| tiny.en (±alle) | 0,80–0,87 | ~70 s |

Alle 14 duer (≥0,80). Kvantisering koster næsten ingenting. Anbefaling: `small.en-greedy`
på GPU, `tiny.en-q5_1`/`base.en-greedy` på CPU — N100-tal mangler endnu.

## Hvor mange samples skal der til?

Probe = 30 s transcript; offset pr. probe; sandhed = fuld-transcript blok-offset.
Træf = probe inden for ±2 s.

| Klip pr. 10 min | Træf |
|---|---|
| 1 | 0,953 |
| 2 | 0,994 |
| 3 | 0,981 |

**2×30 s er sweet spot** (~10 % af lyden). k=3-fejlene sidder ved hop — de er
information, ikke støj. Guards: ≥40 ord pr. probe, ≥20 cues, afvis kantværdier.
Billige modeller holder på E02 (også tiny, dog flere afvisninger ved k=1).

## Detection: rigtig, ude af sync, eller forkert?

Reglen `fil-bedste < 0,65 ELLER probe-spredning > 5 s` gav **16/16 dårlige
flaget, 0/36 ok flaget** — ren adskillelse. Biblioteket indeholdt alle typer:

- **Ok (36):** 0,84–0,96 ved ~0 s.
- **Hop (5):** E21 (+40 s), S03E02 (+18/−15 s), S03E05, S03E07 — reparérbart.
- **Drift (1):** E08 — rigtige ord, vandrende offsets (framerate-type).
- **Forkert indhold (8+):** E14/E16/E20/E22-filerne indeholder naboafsnits dialog,
  E18/E19-backups lige så. Aftale ≤0,15 ved alle offsets — i karantæne, aldrig sync.
- **Belæg:** S03E20-original 0,508 mod CORRECTED-TEST 0,918.

Advarsel: pose-ordoverlap alene ville fejlmatche (naboafsnit scorer 0,59–0,65 på
fælles ordforråd) — **tidsenigheden afgør**.

## Sync over hele filen: alass med video eller whisper som reference?

På E21 (hop-tilfældet), aftale pr. fjerdedel:

- Original: 0,94 / 0,58 / 0,80 / 0,94 (de sidste kræver +40 s).
- alass+video: 0,35 / 0,37 / 0,40 / 0,52 — ødelagt (5 forvirrede blokke).
- alass+whisper-SRT: 0,94 / 0,94 / 0,90 / 0,95 — fikset.
- Direkte blok-shift (uden alass): 0,94 / 0,63 / 0,79 / 0,95 — grovere i rodet midte.

Rene filer lades urørt af alle metoder (også med `small.en`-reference).
Konklusion: **behold alass, skift dens reference til whisper-SRT**; reparér kun
ved verificeret forbedring (E08/E14-typerne må aldrig overskrives).

## Dansk (Stormester, 4/8 afsnit)

Detektion 8/8 dansk (p≈0,99) — men kun på talestykker; filstart gav fejlagtigt
engelsk (p=0,69, musik/intro). Transcript-kvaliteten er flot ved øjesyn, og 8
manuelle holdpunkter i E01–E04 ligger alle inden for ~1 s. Maskinel aftale,
E05–E08 og `small`-på-dansk mangler (se Hullerne). Dansk kræver egen ord-regex
med æøå.

## Adaptiv fortætning (designet, UTESTET)

k=3 slår ikke k=2, fordi faste tætte prober oftere lander oven i hop/stilhed.
I stedet eskalerer flowet selv (`escalate()` i `localsync.py`, testet på
E21-hoppet 2026-09-15):
uenige nabo-prober (>5 s) → én ekstra probe midt imellem → gentag (max dybde
3, max 8 ekstra klip). 4 prober over 20 min er nok til DETEKTION; præcis
placering købes med 2–4 klip kun omkring hoppet. Stige: k=2-screen →
eskalering → full kun ved flag → sekvens-match kun ved swap-mistanke.

## Linjebytte i cues: ikke målt

`swap_eval.py` (syntetiske byttede to-linjers cues, sekvens-matching efter 40 %
falske positiver med median-metoden) findes kun som `.pyc` uden resultater.
Hverken alass eller ordoverlap kan se linjebytte — sporet er uafsluttet.

## e2e: 52 afsnit × 4 scenarier (målt 2026-09-15, koden med P0+VAD)

Raske filer: clean 35/35 ok uden falske alarmer · +45 s shift fikset på alle
35 · line_swap fanget på 29/35 raske (1 miss: S03E19) + alle 9 forkerte
stadig SUSPECT. Forkerte filer: 40/47 urørte+SUSPECT, 7 omskrevet-men-flaget
(backup slået til som default). Hop-filer (8 stk.): alle fikset via
anker-regioner + ok. alass+whisper slår alass+video på alle 6 hop-filer
(video forværrer S03E02: 18→37 s); på forkerte filer fejler begge korrekt;
på rene filer rører ingen noget (±1 s). Detaljer: `verifyarr_patches/`
(`e2e_merged.jsonl`, `alass_ref_after.json`).

## Ikke-uniforme offsets (målt 2026-09-15, 8 raske afsnit, koden med P0+VAD)

Den gamle e2e testede kun +45 s på hele filen. Her er tre ujævne fejl, seeded
(`offset-v1`, 8 parallelle workers, ingen ny whisper — korruption på SRT +
fixture-shim):

- **Hul (5 min midterstykke slettet):** 8/8 perfekt — 100 % af de overlevende
linjer ≤1 s (p50 ≤0,3 s), alle flag ok. Gode linjer røres ikke.
- **Drift (2 % progressiv, fps-type):** 8/8 flag ok, men løst: p50 ~1,0 s,
kun ~47 % ≤1 s, ~80 % ≤2 s. Ses og rettes i grove træk, bliver ~1 s for løs.
- **Stykkevis (6 blokke á ±5–15 s):** først 5/8 flot, 3/8 fejlede (C_S02E09,
C_S03E09, C_S03E16: p50 ~7 s) — alle 3 dog flaget SUSPECT, ingen tavse fejl.
Årsag fundet: ankerplanen blev nedlagt veto af ét enkelt anker pr. blokgrænse
— et klip der dækker grænsen måler medianen af to offsets (mellem naboerne)
og udløste "tynd region = hele planen forkastes". Fix (patch 0004): straddlere
droppes, resten verificeres tæt som før. Efter fix: **7/8 flot** (p50 ≤0,5 s,
79–92 % ≤1 s); C_S03E09 delvist (p50 7,1→1,5 s, 11→35 % ≤1 s) — tætte blokke
(+8,1/+5,3 s) oversegmenteres i 8 regioner, verificeringen er blind ved ~1–2 s.
Øvrige 21 kørsler (drift/hul/raske) byte-identiske: ingen regression.
Før-måling gemt som `e2e_offset_before_fix.jsonl`.

Konklusion: du havde ret i at whisper-timestamps indeholder svaret — men der
manglede ikke flere transskriptioner (55 ankre var der allerede); fejlen var
veto-reglen. Næste skridt: C_S03E09's oversegmentering ved tætte blokke.
Detaljer: `verifyarr_handoff/` (`e2e_offset.jsonl`, `e2e_offset.py`,
`e2e_offset_parallel.py`).

## Hullerne (ærligt)

Dansk er målt maskinelt på alle 8 Stormester-afsnit (large-turbo `-l da`):
aftale 0,81–0,85 ved ~0 s, alle ok, dtw_frac 1,0, mean_p ~0,83 (lavere end
engelsk ~0,91, som ventet). Udestår: `small`/medium på dansk · N100-tider · danske tærskler
(0,65/5 s er engelsk-kalibrerede) · S03E19 swap-misset · `-mc`-test kun på
2 filer. Flowet ligger kørende-klart i `localsync.py` (testet T1–T5).
