# Testmatrix-konklusion: 15 modeller × 10 afsnit × 6 scenarier × 2 modes × 2 audio-confirm

Datasæt: `tests/e2e_matrix.jsonl` — **3600 rækker, 3552 ok, 48 skipped, 0 errors**,
kørt som 150 (afsnit × model)-shards med 16 workers. De 48 skipped er to
sweep-kombinationer, ikke pipeline-fejl: `medium.en-q5_0 / SH_S01E03` mangler
helt (kendt upstream whisper.cpp-crash), og `base.en-greedy-cpu / SH_S01E05`
indeholder én ugyldig byte (`0x8f` i transskriptionsteksten, ca. 16:36) så filen
ikke kan UTF-8-dekodes. Begge falder ud som `skipped`, begge skal re-køres når
sweep-data er fikset upstream. Summary: `tests/e2e_matrix_summary.json`.

Læsevejledning: hvert punkt har det tal det hviler på, og en usikkerhedsnote.
"le1s" = andel cues med restfejl ≤ 1,0 s efter sync.

## A. Kodeændringer — noget kun kode kan rette

### A1. Drift (2 % stræk) kræver rate-/skalakorrektion, ikke flere stykvise offsets
- Full: le1s **0,32**. Sampled (produktionens default): le1s **0,055** — drift
  håndteres i praksis slet ikke i den opsætning der skibes.
- Fejlen er ens på alle afsnit (full le1s 0,17–0,42) og alle 15 modeller
  (0,77–0,82 samlet) — det er pipeline-mekanikken, ikke evidenskvaliteten.
- Eskaleringen opdager problemet (fyrer på 224/296 sampled-drift-rækker) men
  redder det ikke: eskalerede rækker ender på le1s 0,047. Full-transskriptet
  hjælper ikke når fixeren kun kan stykvise offsets.
- Konklusion: byg en rate-komponent (estimér strækfaktor, ikke kun offsets).
  Flere klip løser det ikke — se B3.

*Usikkerhed: kun én drift-rate (2 %) og én mekanisme (lineært stræk) er testet.
Den ægte drift-case C_S03E04 (−0,048 s/min) detekteres i øvrigt slet ikke som
drift (60/60 `already in sync`, 0 SUSPECT på clean) — detektionsgulvet ligger
over ægte smådrift, så jagt ikke sub-0,05 s/min.*

### A2. Swap-fixeren restaurerer for lidt og rører for meget — rekalibrér `_judge_order`
- Injicerede swaps restaureret (audio on): full **0,40**, sampled **0,15**.
  Med audio off: 0,00 — forventet, da off kun flagger.
- Samtidig rører fixeren i full **21,2 ikke-injicerede cues pr. kørsel**, og
  clean→swap-deltaet er kun median **+2**: de injicerede swaps drukner i
  omskrivninger fixeren alligevel ville lave.
- Krydsmodel-enigheden over fiksede indeks er med 15 modeller nede på
  Jaccard **0,02–0,12** på Community (valideringens ~0,4 med 3 modeller
  holdt ikke når N voksede) — modellerne er enige om heuristikken
  (flagget-mængden med audio off er identisk, Jaccard 1,0 overalt), men
  audio-bekræftelsen divergerer. Støjen ligger i `_judge_order`
  (`SWAP_MARGIN = 0,15`) og/eller i tærsklen (min. 3 bekræftede, ≥ 30 %),
  ikke i kandidatfundet.
- Konklusion: stram `_judge_order` før der autofikses mere — se B1.

*Usikkerhed: kun én swap-type (in-cue L1/L2-vending, n = 6) er testet. At
"ikke-injicerede fixes" = falske positive forudsætter at Community-filerne er
sunne — se B2.*

### A3. Linjeorden-falske-positive er bekræftet — samme rodårsag som A2
- Se A2-tallene: 21 uskyldige omskrivninger pr. full-kørsel, Jaccard 0,02–0,12.
  Det er ikke et separat fund, men samme kalibreringsfejl set fra
  clean-siden. Ingen selvstændig fix; løs A2.

## B. Opsætning / flow — koden er fin, indstillingen er forkert

### B1. Behold `line_order_audio_confirm = False` som default (skift ikke til True)
- Med `on` autofikses 40–61 cues pr. Community-afsnit i full — men modellerne
  er uenige om hvilke (Jaccard 0,02–0,12). De kan ikke alle være ægte fejl i
  filen; hovedparten er modelartefakter. Default `True` ville lydløst
  omskrive dusinvis af uskyldige cues pr. fil.
- Med `off` fikses intet, og de flaggede mængder er **identiske på tværs af
  alle 15 modeller** (Jaccard 1,0) — review-køen er deterministisk og stabil:
  7–19 cues pr. Community-afsnit, 0–9 pr. Slow Horses-afsnit.
- Konklusion: default `off` (flag-only) indtil A2 er løst. Genovervej først
  når krydsmodel-enigheden over fiksede indeks er tæt på 1, ikke 0,1.

*Usikkerhed: anbefalingen beskytter mod falske positive på bekostning af ægte
fund — de 2–11 enstemmige cues pr. Community-afsnit er sandsynligvis ægte og
bliver i dag kun flagget, ikke fikset.*

### B2. Community-vs-Slow Horses-forskellen sidder i filerne, ikke i HI-kilden
- Clean full.on: Community **40–61** autofiks pr. afsnit, Slow Horses **0–2**.
  Forskellen står på alle 15 modeller og begge `.en`- og `.en.hi`-filer
  (C_S03E10 er `.hi` med 45 fixes; SH-`.en`-filer får ~1) — så det er ikke
  HI-undertekster eller kilden.
- Mekanikken: Community-filerne indeholder langt flere to-linjers cues der
  tripper `_cap_signal`-heuristikken. Om de er ægte fejl eller støj afgøres
  af A2-tallene: enigheden er 0,02–0,12, så hovedparten er støj.
- Konklusion: lyt et stikprøvehold af de enstemmige cues (2–11 pr. afsnit) igennem
  før Community-rettelserne tolkes som ægte fund. Ingen kodeændring.

*Usikkerhed: 4 Community- mod 6 Slow Horses-afsnit; seriestil (dialogtæthed,
 tegnsætning) er ikke adskilt fra filkvalitet.*

### B3. Behold `sample_count = 5`, `clip_seconds = 60` — drift undtaget
- Piecewise sampled: le1s 0,68, og eskalerede rækker når 0,73 mod 0,49 for
  ikke-eskalerede — 5×60 s + eskalering bærer piecewise.
- Drift sampled: 0,055 uanset eskalering (A1). Fem klip mere ville give samme
  mur: problemet er manglende rate-estimering, ikke dækning.
- Eskaleringen fyrer aldrig forkert: 0/1184 sampled-rækker på
  clean/uniform/swap/gap. Den er gratis at beholde.
- Konklusion: rør ikke sampling-tallene; løs drift i koden (A1).

*Usikkerhed: sampled-klip er skåret ud af full-transskriptet, ikke
ny-transskriberet — ægte kortklips-støj er ikke målt.*

### B4. Asymmetriske sync-tærskler: lad være — der er intet at hente
- På 592 clean-rækker foreslår alass **nul** skift overhovedet
  (hverken +0,4 eller −0,4 s). `min_change_seconds` (0,25 s) og
  `ANCHOR_RESYNC_MIN_SHIFT_S` (1,0 s) bider aldrig i sunde filer, så en
  højere "ryk senere"-grænse ville ikke ændre en eneste clean-beslutning.
- Konklusion: behold symmetriske tærskler. Udgiften ved små positive skift
  er hypotetisk; udgiften ved drift (A1) er målt.

### B5. Modelvalg: den billigste er god nok — tag `tiny.en-greedy-cpu` / `small.en-greedy`
- Sync-kvalitet (le1s over alle timing-scenarier): alle 15 modeller mellem
  **0,77 og 0,82**. Størst (medium.en, 0,80) slår ikke mindst
  (tiny.en-greedy-cpu, **0,82**, bedst i feltet). Swap-restore varierer
  0,11–0,17 uden sammenhæng med størrelse. Selv turbo-fixturen
  (large-v3-turbo, 0,79) matches af lokale modeller.
- Køretider (`sweep/tider_{gpu,cpu}.csv`, rc = 0): GPU hurtigst er
  small.en-greedy (**44 s** snit) og turbo-q5_0/q8_0 (61 s); CPU hurtigst er
  tiny.en-greedy-cpu (**102 s** snit) mod fx base.en-cpu (193 s).
- Konklusion: kør `small.en-greedy` hvor der er GPU, `tiny.en-greedy-cpu`
  hvor der kun er CPU. Store modeller køber intet målbart.

*Usikkerhed: tiderne er transskriptionstid pr. afsnit på sweep-maskinen, ikke
denne maskine; kvaliteten er pipeline-slutresultat, ikke transskript-WER —
en model med bedre ordtider kunne stadig hjælpe A1/A2 indirekte.*

## C. Ikke et problem — virker, jagt det ikke

- **Uniform (+45 s) og gap (5 min klippet ud): le1s 1,00 i begge modes, alle
  modeller.** Ingen spredning at optimere på.
- **Clean-timing:** p50 0,0 s, le1s 1,00 i begge modes — pipelinen rører ikke
  sunde filers timing (untouched: full 185/296, sampled 241/296; resten er
  linjeorden-flags, ikke timing).
- **Eskalering:** fyrer kun hvor der er noget at hente (drift 224/296,
  piecewise 206/296) og aldrig på clean/uniform/swap/gap. Mekanikken er sund;
  det er drift-fixeren den eskalerer til der mangler (A1).
- **Forbeholdene fra handoff holder og skal ikke bygges:** baggrundstale
  (estimatet ved loftet, median −0,06 s, bedste filter AUC ~0,70 kasserer mere
  end det gavner), sangtekst (10,3 % fejlrate men kun 4,5 % af alle fejl,
  koncentreret i to numre; HI sang↔sang-anchors fejlfri 30/30), og
  klipgranularitet (5×60 s holder; ~2 % konfidente klip med |median| 1,0–1,5 s
  ligger under SUSPECT 2,5 s og eskalering 5,0 s). Rådata i
  `whisper_gpu_staging/verifyarr_handoff/`.

## Forbehold for hele dokumentet
- 10 afsnit (4 Community + 6 Slow Horses): afsnitsvariation er reel (drift
  full spænder 0,17–0,42; piecewise 0,54–0,99 pr. afsnit) — små forskelle
  mellem modeller (< 0,05) er støj.
- Én drift-rate, én swap-type, ingen kombinerede scenarier i defaults
  (`drift_swap` er opt-in og ikke kørt her).
- Sampled-klip er udskæringer af sweep-transskripter, ikke live kortklips-STT.
- 48 skipped-rækker (2 sweep-kombinationer, se toppen) — re-kør når upstream
  har leveret/fikset filerne; `skipped` tælles ikke som færdig, så resume
  samler dem op automatisk.
