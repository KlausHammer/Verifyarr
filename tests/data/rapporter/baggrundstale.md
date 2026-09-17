# Baggrundstale som sync-bevis (opgave E)

**Konklusion: byg det ikke.** Baggrundstale findes (ca. 4 % af indholdsbærende
segmenter er rent udækkede, ~5 % af spilletiden), og dens mekanisme står for
næsten tre fjerdedele af de forkerte anchors. Men den kan ikke detekteres
pålideligt med de data produktionen har — det bedste tilgængelige signal
(segmentkonfidens) har AUC ~0,70, og alle afprøvede filtre kasserer mere data
end de gavner: anchor-estimatet er allerede ved loftet (median −0,06 s,
MAD 0,31 s; klipniveau 89/90 konfidente med median |shift| 0,19 s, uændret af
alle filtre). Ordniveau-anchors gør det decideret værre (median flytter +0,66 s,
andel uden for ±1 s stiger 16 % → 27 %). Et negativt resultat med tal bag —
detaljerne følger nedenfor. Rådata: `baggrundstale.json`.

Målt på alle 30 facit-egnede afsnit (9 brugerbekræftede + 21 øvrige; resultaterne
er ens på tværs — CORE9/ALLE30 nedenfor viser det). 13.749 segmenter, heraf
11.764 med indholdstokens; 7.352 accepterede anchors.

## Vigtig forudsætning: hvilket offset er "i sync"?

Tillid-tabellens `best_offset_s` (+1,1…+2,1 s) er **ikke** en sync-fejl — det er
cue-start→ord-midt-lag: cues står typisk ~1–2 s før ordene midt i cuen udtales
(verificeret: ord-agreement topper ved shift ≥+1,7 s, mens segment-start vs.
nærmeste cue-start har median −0,27…+0,21 s på alle 30 afsnit). Anchor-sammen-
ligningen (`segment_start − linje_start`) er derfor regnet mod offset 0, med
"korrekt" = |residual| ≤ 2,5 s (`ANCHOR_SUSPECT_THRESHOLD_S`). Havde jeg brugt
tillid-offsettet, ville alle residualer være biaset −1,7 s — det var min første
kørsels fejl, fanget og rettet før tallene her.

Metode i øvrigt: `_match_segments_to_lines` replikeret tro mod koden (≥0,5
overlap, ≥2 fælles, strengt `>` i linjerækkefølge så tidligste linje vinder ved
lighed, én anchor pr. linje fra tidligste segment), men med kandidater begrænset
til ±10 s (produktionen bruger klip af minutter — fjern-dobbeltgængere er målt
separat, se svaghed 3). Coverage er span-baseret: et whisper-ord er dækket hvis
samme content-token står i en cue hvis [start−1 s, slut+1 s] rummer ordtiden.

## De fire svagheder: be- eller afkræftet

1. **Segmentgrænsen sættes af baggrundstalen — delvist bekræftet, men halen,
   ikke midten.** Lead (segmentstart → første fælles ord) har median 0,70 s;
   66 % ≥ 0,5 s, 32 % ≥ 1,0 s, 16 % ≥ 1,5 s. Men det meste er normalt
   ord-forsinkelse-inde-i-cuen (lead-median 0,7 s ved residual ≈ 0), ikke
   baggrund. Den farlige hale findes: corr(lead, residual) = −0,76, og
   lead≥1,5 s-gruppen (16 % af anchors) har residual-median −1,14 s, MAD 0,74 —
   og rummer **193 af 266 forkerte anchors (73 %)**. Mekanismen er reel, men
   samlet er kun 3,6 % af anchors forkerte, og medianen er uberørt (−0,06 s).
2. **Korte linjer er sårbare — afkræftet i praksis.** Eksemplet holder ikke:
   `tokenize("Yeah, right.")` = ∅ (begge er stopord) — den linje kan slet ikke
   anchore. Linjer med 2 tokens: 3,9 % forkerte mod 3,3 % for linjer med ≥4
   (AUC 0,518 = lodtrækning). Skærpet krav (≥3 fælles for små linjer) kasserer
   42 % af alle anchors uden at flytte MAD (0,309 → 0,306). Frarådes.
3. **Ingen margin — bekræftet, men lille effekt.** Ties (margin 0): 14,4 % af
   anchors, fejlrate 4,7 % vs. 3,4 % uden tie (margin-AUC 0,591). Fjern-
   dobbeltgængere (samme/godt overlap >10 s væk — det produktionens brede vindue
   ville se): 5,5 % af anchors med 8,9 % fejlrate (2,5× grundraten), men dækker
   kun 14 % af de forkerte. Margin-krav fjerner 14 % data for MAD 0,309 → 0,304.
   Marginalt — kan tages eller lades væk.
4. **Konfidens bruges ikke — signalet findes, men er ikke værd at bruge.**
   Segmentkonfidens skiller (AUC 0,70 anchor-niveau / 0,73 segment-niveau;
   dækket vs. udækket ord-median 0,92 vs. 0,70), og `conf ≥ filmedian − 0,1` er
   det bedste billige filter (fjerner 27 % af de forkerte for 8 % datatab,
   std 1,11 → 0,99). Men klip-estimatet flytter sig ikke (se C). Bemærk:
   `no_speech_prob`/`avg_logprob`/`compression_ratio` findes **ikke** i
   out-data (whisper.cpp har kun token-`p`); i produktion bevares de via
   `_normalize_segment` hvis provideren leverer dem — men der er intet at
   hente, estimatet er ved loftet. Serieforskel i øvrigt reel (Community-
   median 0,885 vs. Slow Horses 0,838), så absolutte tærskler ville ramme skævt.

## A. Problemets størrelse

- Rent udækkede segmenter (cov = 0): **499 (4,2 %)**, medianvarighed 2,0 s,
  p90 5 s, max 30 s; total 1.826 s ≈ 61 s/afsnit ≈ **5 % af spilletiden**.
  Eksempler: crowd-/mellemreplikker med høj konfidens ("Someone asked you. You
  guys are being bitches.", conf 0,92; "Oh, sweetie, you don't owe us anything",
  conf 0,97) — selvsikkert transskriberet tale som underteksten udelader.
- Halvt udækkede (cov ≤ 0,5): 1.024 (8,7 %).
- 62,5 % af indholdsbærende segmenter bliver til anchors; heraf har 16 %
  lead ≥ 1,5 s (svaghed-1-halen ovenfor). Coverage-labelen er i øvrigt udvandet
  af gentagne replikker (samme ord i mange cues): baggrundsdominerede anchors
  med cov ≤ 0,5 (n = 126) har residual-median +0,01 s — de fanges ikke af
  coverage, men af lead (som kræver ordtider).

## B. Signalerne — hvad skiller, hvad er virkningsløst

AUC mod "udækket segment" (n = 11.764), ved fornuftig tærskel præcision/recall:

| Signal | AUC | Tærskel | P / R | Dom |
|---|---|---|---|---|
| ord-middel `pmean_c` (lav = baggrund) | 0,745 | < 0,8 | 0,23 / 0,56 | svagt-moderat, bedste billige |
| segment-konfidens (lav = baggrund) | 0,725 | < 0,7 | 0,35 / 0,27 | svagt-moderat |
| `pmin_c` | 0,639 | < 0,5 | 0,13 / 0,64 | svagt, for mange falske |
| taledensitet ratio (høj = baggrund) | 0,591 | > 2,0 | 0,09 / 0,85 | **virkningsløs** (næsten alt over tærskel) |
| uden for VAD-tale | 0,585 | — | 0,11 / 0,70 | **virkningsløs** |
| surplus | 0,416 | — | — | **virkningsløs** (værre end lodtrækning) |

AUC mod "forkert anchor" (n = 7.352, 266 forkerte): lead-orakel 0,772
(**kræver ordtider — utilgængelig i produktion**); seg-konfidens 0,700;
konf-vs-filmedian 0,684; segment-cov 0,615; margin 0,591; tie 0,523,
linjelængde 0,518, VAD-lead 0,487, ratio 0,320 (inverteret) —
**alle virkningsløse som klassifikatorer**.

## C. Hvad ville filtrering betyde? (den afgørende test)

Baseline samlet residual: median −0,055 s, MAD 0,309 s, std 1,114 s.

| Filter | Droppet (heraf forkerte) | Median | MAD | Std |
|---|---|---|---|---|
| ingen (baseline) | — | −0,055 | 0,309 | 1,114 |
| kræv margin > 0 | 1.060 / 14 % (50) | −0,055 | 0,304 | 1,101 |
| kræv margin ≥ 0,2 | 1.169 (58) | −0,054 | 0,299 | 1,084 |
| ≥3 fælles for små linjer | 3.089 / **42 %** (121) | −0,067 | 0,306 | 1,082 |
| konf ≥ filmedian − 0,1 | 556 / 8 % (73) | −0,050 | 0,300 | 0,988 |
| VAD-lead < 3 s | 1.329 (86) | −0,027 | 0,305 | 1,053 |
| ratio < 2,0 | 6.174 / 84 % (155) | −0,122 | 0,360 | 1,724 (værre!) |
| kombi (margin + små-linje + ratio<3) | 5.425 / 74 % (174) | −0,085 | 0,327 | 1,263 |
| lead < 1,0 s (orakel, ikke byggbart) | 2.431 (238) | +0,050 | 0,236 | 0,598 |

Klipniveau (emuleret `_robust_clip_shift`: tredjedele, min. 3 anchors,
MAD ≤ 1,0): baseline **89/90 konfidente klip, median |klipmedian| 0,19 s** —
identisk efter margin-filter (90/90, 0,19 s) og kombi (89/90, 0,20 s).
**Intet filter forbedrer estimatet: de er tab af data.** Højere
`ANCHOR_MIN_SHARED_TOKENS` for korte linjer frarådes eksplicit (42 % tab,
ingen gevinst). Margin-krav er uskadeligt men næsten virkningsløst.

## D. Ordniveau-anchors

Første-fælles-ord i stedet for segmentstart (n = 7.260): MAD 0,303 → 0,323
(**ikke bedre**), median −0,05 → **+0,66 s systematisk bias** (cue-lead: ordene
kommer efter cue-start), andel |resid| > 1 s 15,8 % → **27,2 %**. Median-ordet
er værre (median +1,14 s). Std falder godt nok (1,05 → 0,70), men halerne
klipper den robuste median/MAD-estimator i forvejen — spredningsgevinsten kan
ikke indløses, mens biasen er reel. **Negativt: byg ikke.**

Realisme: produktionens segmenter har ingen ordtider (`_normalize_segment`
beholder kun start/end/text + konfidensfelter). Vejen er Groq
`timestamp_granularities: ["word"]` eller lokal whisper.cpp `-dtw`, plus
plumbing af ordtider gennem normalisering og anchor-match — moderat arbejde
for et negativt resultat. Står ikke mål med gevinsten (der ingen er).

## Forbehold

- Kun i-sync filer: samspillet mellem baggrundsstøj og reel sync-fejl er ikke
  målt (kan pr. design ikke skelnes her).
- Kandidatvindue ±10 s vs. produktionens minutbrede klip: den reelle
  tie/dobbeltgænger-rate i produktion kan være højere; fuldvindue-tjekket
  (5,5 % fjerne dobbeltgængere) sætter en øvre grænse.
- Coverage er udvandet af gentagne replikker; "ren baggrund" (4,2 %) er et
  konservativt mål.
- Data er whisper.cpp large-v3-turbo med token-`p`; produktionens Groq-segmenter
  har andre (rigere) konfidensfelter, men retningen — svagt signal, loft ramt —
  ændres næppe af det.
- VAD-silence som baggrundstegn konfunderes af sang (jf. DATAFORMAT).

## Reproduktion

Analyse (scratch, ikke del af leverancen): `/tmp/bg_analyse.py` (episode-
modellering) + `/tmp/bg_eval.py` + `/tmp/bg_follow.py`, kørt med projektets
`.venv`-python (pysubs2). Kun læst: `verifyarr/subtitles.py`
(`tokenize` importeret direkte), `generate.py` (`_CONFIDENCE_FIELDS`,
`_normalize_segment`), `out/`, fixtures, media-undertekster. Intet commitet,
`out/`/`sweep/`/`wav/` urørt.
