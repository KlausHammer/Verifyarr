# Klipgranularitet: holder E og F ved 5 klip à 60 s?

**Konklusion: ja, begge "byg det ikke" holder — med én præcisering.** Ved
produktionens granularitet (5 klip à 60 s, samme data/matchning som E/F) ændrer
intet byggebart filter estimatet til det bedre; margin-krav og kombi-filteret
gør det decideret værre. Præciseringen: E's tredjedels-måling viste ingen
falsk-positive klipmedianer, men ved 5×60 s findes der 2–4 konfidente klip af
150 med |median| lige over 1,0 s — alle drevet af E's lead-hale (svaghed 1),
højkonfidente og uden ties, så ingen byggebar regel fanger dem. Kun
lead-oraklet (ordtider, utilgængelige i produktion) fjerner dem. Risikoen er
afgrænset: alle ligger under 2,5 s (SUSPECT) og langt under 5,0 s (eskalering) —
værste udfald er en unødig ~1 s-omskrivning, aldrig en falsk eskalering.
VAD-placering søger ikke musik (0/150 regioner med mere sang end
dialogtæthed), og Slow Horses knækker ikke (30/30 konfidente, 0 FP).
Rådata: `klipgranularitet.json`.

Metodevalidering: E-totaler reproduceret eksakt (7.352 anchors, 266 forkerte;
tredjedels-sanity 89/90, median|shift| 0,189) og F-totaler eksakt (7.470
anchors, 289 forkerte; 41 seg-♪, 101 linje-♪, 126 enten; 17 HI/13 ikke-HI).
Placering er reimplementeret tro mod `pick_dialogue_dense_time` /
`pick_sample_time` (NUDGE 0,±10,±20,±30; min. 2,0 s tale; cue-overlap krævet),
5 lige brede regioner over videovarigheden. Heuristiske slots ikke emuleret
(kun filler). VAD-tidslinjer: `out/*.vad.tsv`.

## Del 1 — klipniveau ved 5×60 s (E-track, turbo)

Anchors pr. klip (dialog-strategi): min 5, p10 9, median 14, middel 14,2,
max 22. Opgavens frygt (5–12 anchors, én forkert = 20 %) indtræffer ikke:
**alle 150 klip når ANCHOR_MIN_COUNT = 3**, ingen klip har under 5 anchors.
VAD-strategien er næsten identisk (middel 13,8); 0/150 VAD-klip falder ud som
"ingen evidens", og 88/150 VAD-klip er bit-identiske med dialog-klippet
(median-nudge 0 s, middel 7,1 s).

| Strategi | Konfidente (MAD ≤ 1,0) | Dropout | FP (‖med‖ > 1,0) | Median ‖shift‖ | Spread med/max |
|---|---|---|---|---|---|
| dialogtæthed | 147/150 | 3 (alle MAD 1,01–1,09) | 4 | 0,22 s | 0,42/1,37 s |
| VAD-nudget | 148/150 | 2 | 2 | 0,21 s | 0,42/1,23 s |

Ingen fil har spread over 5,0 s (eskalering udløses aldrig på i-sync filer).

Filtre ved denne granularitet (alle/150 klip; VAD i parentes):

| Filter | Konfidente | FP | Spread med | Dom |
|---|---|---|---|---|
| ingen (baseline) | 147 (148) | 4 (2) | 0,42 (0,42) | — |
| margin > 0 | 147 (144) | **7 (3)** | 0,51 (0,46) | værre: flere FP, flere dropouts |
| ≥3 fælles, små linjer | 149 (144) | 3 (0) | 0,64 (0,57) | ±1 FP for 42 % datatab + større spread — tab af data |
| konf ≥ filmedian − 0,1 | 147 (147) | 3 (1) | 0,45 (0,41) | marginalt, intet estimat-løft |
| kombi (margin + små + ratio<3) | **100 (97)** | 6 (3) | 0,48 (0,42) | kollaps: en tredjedel under evidensgrænsen |
| ♪-linje udelukket (F) | 146 (147) | 4 (2) | 0,42 (0,42) | bogstaveligt ingen effekt |
| lead < 1,0 (orakel) | 144 (145) | **0 (0)** | 0,29 (0,28) | eneste der virker — kræver ordtider, kan ikke bygges |

De 4 dialog-FP-klip (C_S03E08/0: −1,41; C_S03E13/1: −1,02; C_S03E17/2: −1,02
ved MAD 0,14 og n=17; C_S03E21/0: −1,04): 6–13 af 9–17 anchors har lead ≥ 1,0 s
(median-lead 1,1–1,7 s), alle højkonfidente, 0–2 ties, ingen ♪. Det er E's
svaghed-1-hale der ved fin granularitet trækker enkelte klipmedianer over
1,0 s — ikke en ny mekanisme. VAD's 2 FP er samme historie (heraf C_S03E17
igen). Lead-filteret fjerner alle FP, men skubber til gengæld 5 klip under
3-anchorgrænsen — også oraklet har en pris.

**E-dom ved 5×60:** "byg det ikke" står for alle byggebar filtre. Nuancering:
tredjedels-niveauet skjulte at lead-halen giver ~2 % konfidente klip med
|median| ∈ [1,0; 1,5] s; det er stadig under enhver handlingstærskel der
betyder noget (2,5 s SUSPECT, 5,0 s spread).

F-track (small.en, samme klip): baseline dialog 145/150 konfidente, FP 4,
spread 0,61/max 4,57 (VAD: 146/150, FP 4). **V1/V2/V3 ændrer bogstaveligt
intet** — samme konfidente, samme FP, samme spread til sidste decimal.
Største spread (SH_S01E06: 4,57) er small.en-mistiming (17 enige anchors
~−4,5 s tidligt i filen; turbo samme klip: −0,36), ikke musik — og stadig
under 5,0. **F-dom ved 5×60:** "byg det ikke" står uændret.

## Del 2 — søger VAD musik og menneskemængder? Nej

Parret pr. region (150 regioner), VAD minus dialogtæthed: sang-sekunder i
klippet −0,007 s (VAD har mere sang i **0/150** regioner); udækket ordandel
±0,000 (VAD højere i 33/150); tale +1,1 s/klip (VAD højere i 61/150 —
nudgen gør hvad den skal). Klip med sang overhovedet: 5/150 dialog (60
sang-sekunder total) mod 4/150 VAD (59 s). Ankerkvaliteten er ens eller
marginalt bedre med VAD (FP 2 mod 4, konfidente 148 mod 147). Temanummeret
(C_S03E03, 49 % af al sangmasse) giver pæne klipmedianer (0,12–0,33 s) på
begge strategier.

Hvorfor udebliver risikoen? Nudgen er maks. ±30 s, **cue-overlap er påkrævet**,
og median-nudgen er 0 s — VAD flytter kun klippet når der er mere tale tæt
på dialogtoppen, ikke ud i score/mængder. **Alternativet (vægtning mod
undertekstdækning) er ikke nødvendigt**; behold taledækning alene.

## Del 3 — Slow Horses knækker ikke

SH dækker 9 % mod Communitys ~24 % (300 s af ~3.230 s vs. ~1.270 s), men
anchors pr. klip er næsten ens (SH-median 13 mod C-median 15/14):

| Serie | Konfidente dialog (VAD) | FP | Spread med/max |
|---|---|---|---|
| Community (120 klip) | 117 (118)/120 | 4 (2) | 0,44/1,37 |
| Slow Horses (30 klip) | **30 (30)/30** | **0 (0)** | 0,40/0,88 |

SH har nul dropouts, nul FP, og VAD-placering ændrer intet (sang-sekunder
0,0 begge strategier; udækket 0,069/0,071). Hvis noget skulle knække, var det
her — det gør det ikke.

## Forbehold

- Samme som E/F: kun i-sync filer; kandidatvindue ±10 s (produktionens
  [start−30, start+90]-vindue kan give flere fjerne dobbeltgængere — E's
  øvre grænse 5,5 % gælder stadig).
- Emuleret transskription: E/F-segmenter binned efter segmentstart; ægte
  60 s-klip transskriberet med Groq segmenterer anderledes.
- Heuristiske slots ikke emuleret; ekstra slots ved mistanke ej heller.
- SH_S01E06-spreadet viser at model-mistiming (her small.en) kan give
  konfidente skæve klipmedianer — fixture-modelspecifikt, ikke produktionens
  transskriber-familie.
