# Sangtekst som sync-bevis (opgave F)

**Konklusion: byg det ikke.** Sangtekst-anchors ER dårligere end gennemsnittet
(10,3 % forkerte mod 3,9 % i grundraten) — men bidraget er forsvindende lille
(13 af 289 forkerte anchors, 4,5 %), koncentreret i to musiknumre, og alle
afprøvede filtre kasserer data uden at flytte estimatet (median/MAD og
klipniveau identiske). Dertil kommer to fund der underminerer selve fixet: i HI
er sang↔sang-anchors **fejlfri (30/30)** — ægte, brugbare beviser som et
uselektivt filter ville destruere; og den transskriber-familie produktionen
bruger (large-v3: turbo 0/13.749 ♪, groq 0 ♪ i 5 fixtures) markerer slet ikke
sang med ♪, så en ♪-regel ville være no-op netop dér — mens umarkeret sangtekst
(julekoralerne nedenfor) smutter igennem enhver ♪-regel. Et negativt resultat
med tal bag — detaljerne følger. Rådata: `sangtekst.json`.

Målt på de samme 30 facit-egnede afsnit og med samme matchning som opgave E
(`_match_segments_to_lines`-replik: ±10 s kandidater, ≥0,5 overlap, ≥2 fælles,
strengt `>` så tidligste linje vinder, én anchor pr. linje, offset 0,
korrekt = |residual| ≤ 2,5 s) — men på fixture-transskriptionen (lokal
small.en), fordi den er den **eneste** der markerer sang med ♪/♫. Absolutte
anchortal er derfor ikke 1:1 med E (7.470 vs. 7.352 anchors, fejlrate 3,9 %
mod E's 3,4–3,6 %) — sammenligningen der tæller er sang mod ikke-sang og
filter-deltaet inden for samme transskription. Alt er opdelt på HI/ikke-HI:
17 HI-afsnit (243 ♪-cues, 2,98 %) mod 13 ikke-HI (17 ♪-cues, 0,24 %).

## 1. Hvor meget sangtekst er der?

- Transskriptionssiden: **304/17.615 segmenter (1,73 %)**, **885/37.015 s
  (2,39 %)** med ♪/♫. Heraf 31 uden content-tokens (`♪ La la la ♪`-typen) —
  de kan aldrig anchore. Nuværende `_NONSPEECH_RE` fanger 1 af 304.
  Til sammenligning: bracket-musik uden ♪ (`[Music]`-typen, allerede filtreret
  i produktion) står for 123 segmenter.
- Koncentration: **C_S03E03 alene står for 149 af de 304 sangsegmenter
  (49 %)** — et musiknummer (seriens tema, The 88's, diegetisk 139–162 s
  plus genudsendelser). Uden den episode: 155 segmenter (0,91 %).
- Undertekstsiden: HI bærer modparten (243 ♪-cues), ikke-HI gør næsten ikke
  (17 cues) — men "næsten" er ikke "aldrig": C_S03E03's `.en.srt` indeholder
  selv temateksten (kursiv + enkelte ♪-cues).
- Anchors i dag fra ♪-linje på den ene eller anden side: **126/7.470
  (1,69 %)** — 41 segmentside (0,55 %), 101 linjeside (1,35 %), 16 begge.
  Kun 41 af 273 indholdsbærende sangsegmenter (15 %) bliver til anchors.

## 2. Er sang-anchors dårligere? (HI/ikke-HI)

| Gruppe | Alle (n, fejl) | HI (n, fejl) | Ikke-HI (n, fejl) |
|---|---|---|---|
| base | 7.470, 3,9 % | 4.016, 3,3 % | 3.454, 4,5 % |
| seg ♪ | 41, **9,8 %** | 30, **0,0 %** | 11, **36,4 %** |
| linje ♪ | 101, **9,9 %** | 96, 9,4 % | 5, 20,0 % |
| begge ♪ | 16, 6,2 % | 12, 0,0 % | 4, 25,0 % |
| enten ♪ | 126, **10,3 %** | 114, 7,9 % | 12, 33,3 % |
| ingen ♪ | 7.344, 3,8 % | 3.902, 3,2 % | 3.442, 4,4 % |

Samme mønster i turbo-transskriptionen (E-data, umarkeret sang):
linje-♪-anchors 100/7.352 (1,36 %) med 6,0 % fejl — HI 94 @ 6,4 %,
ikke-HI 6 @ 0 %. To uafhængige transskriptioner, samme retning.

Mekanismen er kortlagt, ikke blot talt: **alle 4 forkerte ikke-HI
seg-anchors** er C_S03E03's temasegmenter, hvis tidsstempler small.en
sætter 3–9 s forkert (−3,1…−9,0 s residual på ægte lyric-match).
**Alle 9 forkerte HI linje-anchors** er julekoraler i C_S03E10 (+ én
temalinje i C_S02E18), hvor small.en transskriberer stroferne **uden ♪** som
almindelig dialog — indholdet matcher den rigtige lyric-linje, men
segmenttimingen er 2,5–7,2 s skæv. To pointer: (a) hele den målte
overrisiko sidder i to musiknumre — uden C_S03E03 er ikke-HI seg-siden
0/7 forkert; (b) en segmentside-♪-regel fanger **nul** af de 9
koral-fejl, fordi segmenterne ikke er markerede — kun linjesiden ville.

Sang-relaterede fejl er 13/289 (4,5 %) af alle forkerte anchors.

## 3. Det gentagne omkvæd — afkræftet som særskilt fare

Fjern-dobbeltgængere: 399/7.470 (**5,3 %**, E: 5,5 %) med 5,3 % fejlrate
(E: 8,9 % — anden transskription). Heraf sang: **16/399 (4,0 %), alle i HI,
nul i ikke-HI**. Residual-fordeling:

- dobbeltgænger+sang (n=16): fejl 6,2 %, |resid| median 0,40 s, p90 2,01 s,
  max 7,18 s.
- dobbeltgænger+ikke-sang (n=383): fejl 5,2 %, median 0,43 s, p90 1,52 s,
  max 8,39 s.

Ingen systematisk forskel; de store residualer findes i begge grupper, og
max er større uden for sang. De store sang-residualer der ER observeret
(C_S03E03 −3…−9 s, koralerne −2,5…−7,2 s) er **fejl-timed ægte match**, ikke
omkvæd matchet til forkert forekomst — tidligste-linje-ved-lighed-mekanismen
giver ingen målbar ekstra hale her.

## 4. Musik OVER tale — kan ikke afgrænses, og der er intet at hente

- Forslag 1 (VAD-passage med både ♪ og dialog): **14 kandidater på 30
  afsnit → 8 anchors, 0 forkerte.** Populationen eksisterer næsten ikke,
  fordi Silero-VAD inkonsekvent fyrer på sang (C_S02E01/SH_S01E05: 0 % af
  sangtiden i VAD; C_S03E03: 53 %). Kan pr. konstruktion ikke bære et
  filter — og bisætning til E: sang konfunderer VAD-fravær som
  baggrundstegn den anden vej.
- Forslag 2 (naboskab ±15 s om sang, 391 kandidater → 202 anchors):
  fejlrate **2,0 % mod 3,9 % i grundraten** (HI 2,9 %/3,3 %; ikke-HI
  **0/65 mod 4,5 %**). Nær-sang-dialog er ikke værre — om noget bedre.
- Konfidensdip uden manglende undertekst: **findes ikke.** Ordkonfidens
  (out/-ord) median 0,908 i naboskab mod 0,912 alle; og
  lav-konfidens→forkert-AUC **kollapser 0,63 → 0,35** inde i naboskabet
  (HI 0,66 → 0,37; ikke-HI: ingen forkerte i naboskab overhovedet).
  Selv E's svage signal (AUC ~0,70) forsvinder — inverteret — nær musik.
  Tre uafhængige nej.

## 5. Hvad ville en ændring koste og give? (samme målestok som E)

| Variant | Anchors (tabt, heraf forkerte) | Fejlrate | Median / MAD / std | Klip konf. / med\|shift\| |
|---|---|---|---|---|
| V0 baseline | 7.470 | 3,87 % | −0,104 / 0,387 / 1,137 | 89/90, 0,194 s |
| V1 drop alle ♪-segmenter | 7.429 (41 / 4) | 3,84 % | −0,104 / 0,386 / 1,130 | 89/90, 0,195 s |
| V2 drop ♪-seg kun ikke-HI | 7.459 (11 / 4) | 3,82 % | −0,104 / 0,387 / 1,129 | 89/90, 0,194 s |
| V3 drop ♪ begge sider | 7.344 (126 / 13) | 3,77 % | −0,102 / 0,385 / 1,126 | 89/90, 0,195 s |

V2 har bedst præcision (4 forkerte af 11 tabte) og bevarer HI's fejlfri
sang↔sang-beviser — men flytter **intet**: samme median, samme MAD, samme
89/90 konfidente klip. V3 fanger også koral-fejlene (13 forkerte af 126
tabte) til samme nul-gevinst. Efter E's målestok er alle tre **tab af
data**. Byg hverken V1 (ødelægger 30/30 korrekte HI-beviser) eller V3
(126 anchors for 13 fejl, ingen estimat-gevinst); heller ikke V2 — 0,05
pointpoint på fejlraten uden klip-effekt er ikke en ændring værd.

## Forbehold

- Kun i-sync filer: samspil med reel sync-fejl ikke målt.
- ♪-populationen er defineret af small.en's markering; ±10 s-vindue mod
  produktionens minutbrede klip (fuldvindue-dobbeltgængere er målt separat,
  §3). Kursiv-uden-♪-lyriklinjer er ikke flagget — linjesidens ♪-tal er en
  nedre grænse.
- C_S03E03's 149 segmenter dominerer sangmassen; uden den er ikke-HI
  seg-siden fejlfri (0/7).
- Q4's konfidens bruger out/-ordtider lagt over fixture-segmenter (samme
  lyd, anden segmentering) — retningen (intet dip, kollapset AUC) er robust
  over for det, absolutte niveauer mindre.
- Groq-fundet (0 ♪, 0 brackets i 5 fixtures) gælder fixture-udsnittet; om
  sweep-kørslen markerer anderledes er ikke undersøgt (`sweep/` urørt pr.
  reglerne).

## Reproduktion

Analyse (scratch, ikke del af leverancen): `/tmp/song_analyse.py`
(per-afsnit JSON) + `/tmp/song_eval.py` (Q1–Q5) + `/tmp/song_json.py`
(leverance-JSON), kørt med projektets `.venv`-python (pysubs2). Læst:
`verifyarr/subtitles.py` (`tokenize`, `is_nonspeech_annotation`,
`_match_segments_to_lines`/`_robust_clip_shift` som kontrakt),
fixtures, media-undertekster, `out/*.vad.tsv` + `out/*.words.json` som
hjælpetidslinjer, samt `/tmp/bg_all30.json` til turbo-supplementet.
Intet committet, `out/`/`sweep/`/`wav/` urørt.
