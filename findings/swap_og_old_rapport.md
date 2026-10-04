# Swap-gate og old-wins (HEAD d9a7ad2 + to patches)

To patches i rækkefølge, ingen commits: `swap_gate.patch` (816 linjer),
`old_wins.patch` (204 linjer). Arbejdstræet har begge; begge lagt rent på
d9a7ad2 i rækkefølge (verificeret med `git apply --check`).

## 1. Swap-trin: mange byttede linjer → hent ny

Måling før dom: timing-uafhængig scan over alle to-linjers cues (tekstmatch
±90 s + sekvensdom, prototype `swap_scan.py`). Tærskel: rate ≥ 0,10 OG ≥ 5
byttede af ≥ 10 afgjorte. Gaten ligger i `correctness_and_finish` FØR
sync-resolution/fps/resync; ved udslag droppes deferred sync uskrevet,
flag SUSPECT + "Many swapped lines (X of Y tested) -- fetch a fresh
subtitle." + `handle_suspect`. Sampled køber fuldt transskript ved ≥ 6 frie
heuristik-hits (samme mønster som jitter/missing middle).

Tærskeltal (turbo = alle 52 via `swap_scan.json`; tiny = 18 med sweep):

| korpus | rene | med swap | tærskel |
|---|---|---|---|
| turbo (52) | 0–0,067 (28 filer, max 1 byttet) | 0,147–0,54 (24 filer, min 5 byttede, n 34–68) | 0,10 i hullet; umålbare (forkert indhold) n 0–1 udelukkes af ≥ 10 |
| tiny (18) | SH 0–0,014 (n 35–77, 0–1 byttet) | 0,209–0,491 (12 filer, 7–27 byttede) | samme 0,10; hul 0,014–0,209 |
| injiceret n=6 (tiny, SH) | — | rate 0,013–0,050, 1–2 byttede | under alle tre bare |
| injiceret 25 % (tiny, SH) | — | rate 0,200–0,400, 8–25 byttede | over alle tre bare |

Heuristik-trigger: med_swap 7–26 hits, 25 % syntetisk min 6, rene max 9
(SH_S01E02 eskalerer i forvejen via sit 174 s-hul). Bar 6 fanger alle 24 +
25 %; 3 rene køber et spildt kig uden flag.

Verifikation:

- SH-matrix (tiny, 552 rækker, eksakt kommando): 0 flag/sync-forskelle mod
  `sh_mm1.jsonl`. Swap n=6 uændret (rapporteret, ikke "hent ny").
- Nyt scenarie `many_swaps` (25 %, uden for default): 24/24 SUSPECT +
  urørt, intet repareret (forventet: flagges, ikke repareres).
- Genuine alle 52 (104 rækker, turbo): alle 24 `med_swap_udelukket` flagges
  af gaten (48/48 rækker med noten; 0 uden, så ingen liste). 19 afsnit er
  ny-flagget ok→SUSPECT (38 rækker); 5 var SUSPECT i forvejen og er nu
  urørte via gaten i stedet for resync/fps-rettet. `baser` + `uden_swap` +
  `rigtige_uden_swap` (21 afsnit, 42 rækker): 0 gate-flag. Note: C_S02E04/
  C_S02E06/C_S03E04 mister deres fps-rettelse til gaten (korrekt per
  "heller ikke timing" — filen hentes ny).

## 2. Skriv aldrig en kandidat ankrene modbeviser

Rettelser i `_resolve_ambiguous_sync`: single-blok tæller som én blok
[0, ∞) (før: altid False, "old" kunne aldrig vinde); veto når vinderen har
≥ 3 ankre > 2,5 s ude mens originalen er klart bedre på fælles klip OG i
middel — aldrig mod ramp rescue eller klar indholdsforskel (> 0,1).
Veto markeres, kalderen flagger SUSPECT og returnerer uden fps/resync.
Rollback ved SUSPECT generelt er fravalgt: resync-politikken beholder
bevidst forbedringen + flag (testlåst), og veto forhindrer i stedet
skrivningen.

Probe før/efter (54 rækker: jit_small/jit_large/trunc_start150 × 9 afsnit ×
2 modes; før = review-data, efter = begge patches):

- Community 18/18 forbedret (gate + veto): jit_large p50 9–16 s → 3,4 s
  (urørt, ikke ødelagt); jit_small tavse ok+omskrevne → SUSPECT+urørte;
  trunc_start150 ødelagt (Δ115 s, p50 9 s) → urørt p50 0 + flag; 5 tavse
  trunkeringer nu flagget.
- SH 32/36 uændret, 4 veto-afvisninger (jit_large, overfit Δ2,8–13,8 s →
  urørt, stadig SUSPECT; p50 følger injiceret støj, ingen systematisk
  rettelse tabt). 0 nye tavse rækker. Rest: SH_S01E05 jit_large Δ87,5 s
  stadig ødelagt (uændret før/efter).
- SH-matrix (begge patches, 552 rækker): 4 forskelle, alle jitter sampled
  fixed→afvist, stadig SUSPECT. uniform/fps/pal/drift 192/192 rettet.

## Suite og test

262 bestået + 7 subtests, exit 0. Nye: `test_swap_gate.py` (5),
`test_old_wins.py` (4, mockede); begge skrevet før koden, fejlede før,
mutationstjekket (høj tærskel / veto=999 fejler). Eksisterende rørledningstest
flyttet til SH per "indlagte fejl kun på SH": 27 fejl forårsaget af reelle
swaps i Community-fixtures rettet (bl.a. `HEALTHY_SLUGS` = S02E15 alene;
S02E01/06/10 har 14–18 byttede). Genuine-kopi: `genuine_swap_gate.jsonl`.
Probe-efter: `/tmp/probe_after_run/probe_after.jsonl` (+ scripts
`/tmp/swap_tiny_scan.py`, `/tmp/swap_injected.py`, `/tmp/swap_heur_all.py`).
