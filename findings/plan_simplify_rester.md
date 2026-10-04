# Plan: de /simplify-fund der ikke blev rettet i 03943fc

Status: testet 2026-09-27. 9 af 10 beholdt; punkt 9 rullet tilbage (27bd6bc). Ikke pushet. Hvert punkt er sin egen commit,
så det kan rulles tilbage alene med `git revert <sha>`. Punkt 9 har desuden en
indstilling, så den gamle metode kan slås til igen uden revert.

Testplan (når der gives go): randomiseret matrix + standardmatrix + 52 rigtige
afsnit + suite, sammenlignet med rf1/sf1 med `cmp_runs.py`. Punkt 1-8 og 10 skal
give identisk output (0 forskelle). Punkt 9 må ikke tabe nogen rettelse: 240/240
drift/ratio, 312/312 default arm1. Fejler et punkt: `git revert` af den commit
(punkt 9: `sync.rate_legacy_paths = true`) og kør igen.

## Implementeres

| # | Commit | Fund (agent) | Hvad | Forventet effekt | Tilbagerulning |
|---|---|---|---|---|---|
| 1 | 2bdfd58 | Faelles memo (forenkling/genbrug) | `verifyarr/memo.py: BoundedMemo` bruges af VAD-, transskript- og varighedsmemoet (3 kopier i dag) | Ingen adfaerdsaendring | revert |
| 2 | f8642ea | Rate-hjaelpere (genbrug 2-4) | `stretch_ratio(p)`, `stretch_name(r)`, een `probe_gates_pass` for stretch- og rate-gates, `_dense_pool` via `_fps_points` | Ingen adfaerdsaendring | revert |
| 3 | bfd763d | Matrix-korruptorer (genbrug 11) | Faelles `_shift_block`, `_fit_start`, `_drop_span` for blokke/huller | Samme indlagte filer (RNG-raekkefoelge uaendret) | revert |
| 4 | 23a4d42 | Originalen altid sat (hoejde 2) | `_orig_subs` saettes altid i skrivestien; faellesfallback-kaeder fjernes | Ingen adfaerdsaendring | revert |
| 5 | 2abfa16 | Cache-skema (hoejde 5) | Versionsnummer i line-order-cache-noeglen + `_cache_json`/`_cache_load` som par | Gamle cache-raekker uden `full_coverage` genberegnes een gang (klip hentes fra klip-cachen) | revert |
| 6 | e209cd8 | VAD i DB (effektivitet 1a) | Ny tabel `vad_timeline_cache` (sti, mtime, stoerrelse, model). VAD-tidslinjen dekodes een gang pr. video, ikke pr. proces | 30-100 s sparet pr. fil paa N100 ved genkoersler | revert |
| 7 | d556634 | Hook-stien deler lyd (effektivitet 1b) | `_run_single` faar `audio_cache` + tempdir som sweepet: alass og VAD bruger samme WAV | Een dekodning i stedet for to; hook-stien koerer nu som den testede sweep-sti | revert |
| 8 | c63980c | Gap-probe-cache (effektivitet 5) | Ny tabel `gap_probe_cache` (video, start, laengde, model, sprog). Egen tabel -- ikke klip-cachen, som foeder kandidat-evidensen | ~4,6 min Whisper-lyd sparet pr. afsnit ved genkoersler | revert |
| 9 | ce68a8c (rullet tilbage i 27bd6bc) | Een rate-sti (hoejde 7) | Rate fra originalen er eneste rate-fixer efter alass; den diskrete 0,1 %-sti og ramp-rescue springes over | Mindre kode at holde konsistent; skal maales | `sync.rate_legacy_paths = true`, ellers revert |
| 10 | 1f27bc9 | Testdublet (genbrug 12) | `test_rerun_on_cache_keeps_missing_middle` bruger `_run` | Ingen | revert |

## Springes over (med grund)

- **SyncOutcome-dataclass (hoejde 1):** stor omskrivning af tre kaldere; punkt 4 tager
  den del, der fjerner fallback-kaederne. Resten giver ingen maalbar gevinst.
- **Reparationsdele som felt (hoejde 3):** status-teksten overskrives altid, naar filen
  aendres, saa den nulstiller sig selv. Et separat felt skal nulstilles ved hver af de
  7+ steder, status skrives -- mere skroebeligt, ikke mindre. Ordlyden er daekket af test.
- **Kalibreringsprofil pr. model (hoejde 4):** kun tiny.en er understoettet (drop
  ustabile modeller); en profil for modeller, vi ikke koerer, er kode uden brug.
- **Faelles lydkilde i stedet for KNOWN_WAVS (hoejde 6):** ryddes nu efter hvert sweep,
  og punkt 7 daekker hook-stien. Resten er omstrukturering uden gevinst.
- **Evidens hentet een gang (hoejde 8):** memoet goer det allerede billigt (<10 ms/fil).
- **cfg_for(model=) (hoejde 9):** det syntetiske filnavn spejler produktionens noegle;
  omdoebning i 6 kaldere giver intet.
- **JobRunner-laas og conftest (hoejde 10):** in-process-laasen styrer koeen, flock
  styrer processerne -- to formaal. conftest virker.
- **Sammenlaegning af run-detektorer (genbrug 1):** anden afrunding og flette-regel ->
  adfaerdsaendring.
- **dense_anchor_points via clip_anchors (genbrug 5):** clip_anchors regner ogsaa det
  robuste skift -> mere arbejde, ikke mindre.
- **_runs_text til run_offsets (genbrug 9):** aendrer rapportteksten.
- **VAD forudhentet i sync-poolen (effektivitet 2):** efter punkt 6 dekodes kun een gang
  pr. video; poolen har ingen DB-adgang og ville dekode videoer, der allerede er cachet.
- **Spring stille gap-vinduer over med VAD (effektivitet 4):** maalt tidligere -- VAD
  daekker 57-71 % af talen, huller tabte op til 83 ord.
- **Inkrementelt filter i gap-proben (effektivitet 5, lille del):** en gentagelsesloekke
  over en vinduesgraense ville slippe igennem -> adfaerdsaendring.
- **Memo af dense_anchor_points, tokenisering een gang, memo foer DB-laesning
  (effektivitet 7-9):** millisekunder; risiko for foraeldet memo.
- **REAL_RATIOS af NTSC/PAL-konstanter:** sidste decimal aendrer de indlagte filer.
- **Ubrugte parametre (forenkling 8):** harmloese standardvaerdier.

## Testraekkefoelge og kendte risici

1. Suite foerst (6 min). Forventede roede tests, hvis nogen: tests der gaar gennem
   pipelinen og forventer den diskrete 0,1 %-rettelse eller ramp-rescue (punkt 9).
   Hvis de fejler, men matricen er groen: testen skal saette `rate_legacy_paths`
   eller skrives om -- den gamle sti er ikke laengere standard.
2. Randomiseret matrix + standardmatrix + 52 rigtige afsnit mod sf1 med
   `cmp_runs.py`. Punkt 1-8 og 10 maa give 0 forskelle.
3. Punkt 9 alene: forskelle kun paa rate-raekker, og kun hvis den nye sti ogsaa
   retter dem (240/240, p50 <= 0,15). Ellers `sync.rate_legacy_paths = true` og
   maal igen; hvis det heller ikke er rent, `git revert ce68a8c`.
4. Punkt 6-8 kraever ogsaa en genkoersel paa samme DB for at vise besparelsen:
   anden koersel skal have 0 VAD-dekodninger og `cached_audio_s` for gap-proben.
5. Punkt 5 giver een gang genindsamling af gamle cache-raekker i produktionen
   (klippene hentes fra klip-cachen, ingen ny Whisper).

## Resultat (2026-09-27)

- Suite med punkt 9: 3 roede, alle i test_fps_rescale. C_S02E11 (rigtig 0,1 %-drift,
  brugerbekraeftet) blev IKKE rettet af den nye sti: 0,1 % over ~21 min er ~1,3 s
  tilt, under rate-stiens 1,5 s-gulv. Den diskrete sti baerer det tilfaelde ->
  punkt 9 rullet tilbage (27bd6bc), test_fps_rescale 16/16 igen.
- Matricer mod sf1: rand 300/300, default 552/552, 52 rigtige afsnit 104/104 --
  0 forskelle. Suite 357 groen (354 + de 3 efter tilbagerulning).
- Genkoersel paa samme DB (SH_S01E01 sampled, RAM-memo toemt = ny proces):
  koersel 1: 1 lyddekodning, 699 s frisk Whisper-lyd;
  koersel 2: 0 lyddekodninger (VAD fra DB), 17 s frisk / 1132 s cachet
  (gap-proben fra gap_probe_cache).
