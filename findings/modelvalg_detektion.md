# Modelvalg: detektion pr. model (2026-09-29)

Kørsel: HEAD d85517e, alle 15 modeller × 10 godkendte afsnit × 23 standardscenarier ×
full+sampled × audio-confirm off. 6900 rækker: 6854 ok, 0 errors, 46 skipped (kun den
kendte kombination medium.en-q5_0/SH_S01E03, sweep-data mangler). Sampled er
produktionens opsætning og dermed hovedresultatet.
Rådata: `matrixdata/modelvalg_2026-09-29/matrix.jsonl` (+ `_summary.json`).
Alle tal herunder er talt direkte på jsonl-rækkerne, stikprøvekontrolleret mod summary.

Kriterier (samme for alle modeller): RETTET = recovered p50 ≤ 0,25 s og ≥98 % ≤1 s.
Blokke: detekteret = flag != ok (politik 14.38, reparationen bliver på disken).
missing_middle: harnessets `detected`. wrong_episode: SUSPECT + uberørt.
swap/drift_swap (audio off): noticed (kun flag, intet autofiks). dropdup: uberørt.
jitter: recovered p50 ≤ injiceret p50 (ikke værre). clean: uberørt + uflagget.
n angives pr. celle; forskelle på 1-2 rækker er støj.

## Målingen først: Community-timingceller er blokerede

Alle timing-scenarier på C_S03E03/E08/E10 ender identisk for alle 15 modeller:
`left unchanged (many swapped lines)` + SUSPECT. De tre afsnit har ægte byttede
linjer, så line-order-porten nægter al sync før modellen overhovedet kommer i spil.
Disse celler måler porten, ikke synckvalitet — de informative tal er SH-only
(6 afsnit). C_S03E04 (drift-case) har ingen `recovered` og indgår kun i flag-mål.
Huller (hole_rand/trunc_*) er ikke i standardscenariesættet og er ikke kørt (n=0).

## Offset og rate, SH-only (krav: 100 % rettet)

Sampled (produktion):

| model | offset rettet (n=36) | rate rettet (n=36) |
|---|---|---|
| tiny.en-greedy-cpu (prod) | 35 | 36 |
| small.en-greedy | 35 | 36 |
| medium.en-greedy | 36 | 36 |
| øvrige sweep (12) | 32-36 | 36 |
| turbo (cloud) | 36 | 32 |

Full: samme billede (offset 34-36/36, rate 36/36), undtagen turbo 30/36 + 29/36.
n=30 for medium.en-q5_0 (E03 mangler: offset 28/30 sampled, rate 29/30 begge modes).

Fejlcellerne: alle sweep-offset-misses er uniform_p03 (+0,3 s lades ligge ved
0,25-baren, p50=0,3/le1=1,0/already-in-sync) — spredt 0-4 rækker pr. model uden
rangorden, ren støj. Rate-misses: turbo E06 drift/drift_offset (alass Δ55,7 s
katastrofe, le1=0,06; drift_swap sampled afvist, p50=26 s), turbo E04 drift
marginalt over baren (p50=0,27-0,30), turbo-E01-artefaktet (full, se clean),
medium.en-q5_0 E01 fps_late begge modes (kendt 14.24, FP-ankre). Kravet er reelt
nået for alle sweep-modeller; afvigelserne er enkeltceller og kendte artefakter.

## Blokke og manglende midte (krav: 100 % detekteret)

Blokke detekteret: 40/40 (n=36 for q5_0) for ALLE 15 modeller i BEGGE modes —
kravet nået uden en eneste miss. Blokke rettet-timing (info, ikke krav): 9/24
SH-only for alle, begge modes (afvigelser ±1-2 rækker = støj; turbo full 5/24).
missing_middle: 10/10 alle, begge modes — undtagen turbo full 9/10 (E01 rives med
af -3,7 s-artefaktet, filen omskrives så `detected` fejler).

## clean = falske positiver (vigtigste sikkerhedstal)

Alle 15 modeller flagger de 4 Community-afsnit (SUSPECT, byttede linjer, alle
uberørte) — facit-egenskab, ikke modelforskel. Eneste undtagelse: base.en-cpu
lader E10 passere (13 lavkonfidens-flag under porten). SH clean-FP, alle uberørte:

| model | SH clean-FP (n=6/mode) | årsag |
|---|---|---|
| tiny.en-greedy-cpu, small.en-greedy, medium.en-greedy (+9 mere) | 0 begge modes | — |
| medium.en-q5_0 | E01 begge modes | kendt 14.24: -4,5 s FP-ankre |
| tiny.en-cpu (ikke-greedy) | E02 begge modes, E04 full | gap-detektor: tale uden undertekstlinjer |
| turbo | E01 full: FILEN OMSKREVET | kendt 14.26: -3,7 s-ankre → resync |

turbo full E01 (`fixed Δ3,7 s, 3 anchor regions`) er den eneste omskrivning af en
korrekt fil i hele matricen (6854 rækker). Produktionens tiny.en-greedy-cpu har
0 SH clean-FP i begge modes — samme som small/medium.

## Robusthed, wrong_episode, swap

- dropdup: 10/10 uberørt alle, begge modes (turbo full 9/10, E01-artefaktet).
- jitter: full-mode misses E03+E04 er IDENTISKE for alle 15 modeller (alass jager
  støjen: Δ3,7/2,0 s, rec værre end injiceret) — modeluafhængigt. Sampled: spredt
  1-2/model, støj (bl.a. tiny.en-cpu E05 marginalt +0,04 s).
- wrong_episode: 10/10 (SUSPECT+uberørt) alle, begge modes. swap: noticed 20/20
  alle, frac_restored 0,0 alle (audio off → kun flag). drift_swap noticed 20/20
  alle; timing-delen = rate-billedet ovenfor. Ingen modelforskel nogetsteds her.

## Eskalering sampled→full og kørselstid

Eskalering sampled: 71-118/230 (31-51 %) pr. model. Trioen: tiny 79, small 110,
medium 94. På lette filer eskalerer tiny systematisk mindst (clean 0 vs 2/1,
uniform*/fps/swap/dropdup 0 vs 1-3, pal 1-2 vs 4-6) med identisk udfald — færre
spildte fuldtransskriptioner. Hårde scenarier (piecewise/jitter/cut_version/
wrong_episode) 10/10 alle. Full-mode eskalering 0 overalt (kontrol OK).
whisper_cost er 0 i alle rækker: matricen laver ingen Whisper-kald (evidens fra
sweep-transskripter), så harnesset måler ingen transskriptionstid pr. model.
Shard wall-time (delt pipeline-arbejde under parallel last, ingen transskription)
52-79 min pr. model-sum siger intet om modellerne. Reelle priser står i
fund_og_fejl (16 klip: 9 s tiny, 48 s small.en-q5_1).

## Kryds: tiny.en-greedy-cpu vs small/medium

Sampled, 3 uenige rækker af 230: tiny fejler SH_S01E02 uniform_p03 hvor begge
består; tiny består C_S03E10 uniform_neg + SH_S01E04 uniform_p03 hvor small fejler
(E10-cellen er port-lotteri: 4 modeller slap igennem swapped-porten og fik fikset
timingen — inkl. tiny og small.en-q5_1; small.en-greedy/medium.en-greedy blev
lukket ude). Full, 2 uenige: begge tiny foran (samme E10 + E04 uniform_p03, her
fejler også medium). Alle enkeltceller = støj, og nettet peger om noget på tiny.

## Konklusion

Modelvalget ændrer intet for detektion/rettelser mellem sweep-modellerne: på SH
retter alle offset/rate 35-36/36, detekterer blokke 40/40 og missing_middle
10/10, med 0 SH clean-FP for tiny/small/medium-greedy — forskellene er 1-2
enkeltrækker uden rangorden. De eneste udliggere er turbo (cloud-transskriptets
-3,7 s-artefakt omskriver korrekt E01-fil og misser E06-drift) og medium.en-q5_0
(kendt E01-FP). Færre ord (tiny F1 0,75 vs 0,87-0,89) og ~20 % færre ankre slår
ikke igennem i et eneste detektionsmål; til gengæld eskalerer tiny mindst på
lette filer med samme udfald. Valget står mellem hastighed (tiny) og ord-nøjagtighed
(small/medium) — detektionen favoriserer ingen af dem. Ingen anbefaling herfra.
