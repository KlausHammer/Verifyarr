# Skrøbelige grænser, GPU/CPU og alass som sidste trin (2026-10-07)

Spørgsmålet: afhænger udfaldet af for lidt? Når to Whisper-kørsler på samme afsnit (GPU mod CPU, to modelvarianter) giver små
forskelle i ord og tidsstempler, må det ikke vippe en afgørelse. Alle tal er målt på Known Good-sættet (`tests/known_good/`, replay af
optagede transskripter og alass-svar) med mindre andet står.

## 1. GPU mod CPU (Vulkan, Intel ADL-N)

* `verifyarr/gpu_compare.py` og `docker-compose.gpu-test.yml`: klip-tilstand (kun Whisper) og `--pipeline` (hele pipelinen med GPU til
  og fra, begge rettede filer gemmes). Seks afsnit med engelsk tale og tekst; tre OK, tre med kendte fejl.
* GPU er kun ca. 5-7 % hurtigere end CPU på denne maskine.
* GPU giver andre ord og anden segmentering end CPU. CPU mod CPU er identisk. Derfor kan en afgørelse på kanten vippe mellem de to.
* Blue Mountain State S01E10: årsagen til forskellen var rate-porten. Hældningen læste rho −0,424 (GPU) og −0,464 (CPU) mod en grænse
  på 0,45. Det er rettet (afsnit 4).

## 2. Følsomhedsanalyse

`tests/known_good/sensitivity.py` ganger hver numerisk konstant i subtitles/pipeline/correctness/line_order med 0,8 og 1,25 og tæller,
hvor mange celler der skifter udfald (`VERIFYARR_PERTURB="NAVN=faktor"`, understøttet i `tests/e2e_matrix.py`).

**Stor matrix (580 celler, tiny.en-greedy, 106 konstanter):** 81 af 106 konstanter flytter ingen celle. Matrixen indeholder store fejl,
hvor reglerne ikke er på kanten. De mest følsomme (celler der skifter ved 0,8 / 1,25):

| Konstant | 0,8 | 1,25 | Gruppe |
|---|---|---|---|
| RATE_MIN_KEEP | 9 | 88 | rate, offset, blokke |
| STRETCH_RHO_MIN, STRETCH_MIN_KEEP_FRAC | 0 | 41 | rate |
| ANCHOR_MIN_COUNT | 37 | 34 | blokke |
| RATE_TIGHT_RESID_S | 36 | 5 | rate, offset |
| ANCHOR_MIN_OVERLAP | 11 | 34 | blokke |
| RATE_TIGHT_OFFSET_S | 11 | 16 | rate |
| ANCHOR_SUSPECT_THRESHOLD_S | 11 | 3 | blokke, jitter |

**Svage ramper (nyt sæt, 100 celler, `slow_rand0..9`, drift 0,03-0,15 % plus op til 3 s forskydning):** matrixen testede ikke dette
område, hvor rigtige filer ligger (fx Blue Mountain State S01E10). Her er RATE_MIN_KEEP (54 celler ved ×1,25), RATE_MIN_RHO (23) og
ANCHOR_MIN_OVERLAP (18) skrøbeligst.

**Varianter af samme model:** tiny.en-varianterne (greedy, standard, q5_1) giver forskellige udfald på samme celler. Målt før
ændringerne: 13 af 100 svage-ramp-celler er blandede mellem varianterne.

## 3. Hvad der var galt, og hvad der blev ændret

1. **Dødt område for svage ramper (commit 3eb7e60).** Rate-porten krævede tilt ≥ 1,5 s og rho ≥ 0,70 på én gang. Ramper på 0,9-1,9 s
   læser rho 0,37-0,70 og blev derfor kun rettet som forskydning, med 0,3-0,45 s rest. Ny `rate_gate_level` med et svagt trin
   (`RATE_WEAK_*`: tilt ≥ 0,85 s, rho ≥ 0,40, gain ≥ 0,07 s, resid ≤ 0,32 s). Et svagt trin bruges kun, hvis filen bagefter måler
   stramt flad (`rate_is_flat(after, tight=True)`). Margin: alle scenarier uden rate læser tilt ≤ 0,70 s og gain ≤ 0,01 s.
2. **RATE_MIN_KEEP 0,90 → 0,85 (commit fbf8c5b).** q5_1 læser 0,88 på rigtige ramper og blev afvist for 0,02. Blokke ligger fortsat
   ≤ 0,65.
3. **KG-replay bruger nu VAD (commit 431524b).** Replayen kaldte aldrig VAD-bekræftelsen af blok-kørsler, men produktionen gør.
   Datasættets VAD-filer bruges nu. Konsekvens: rene filer består 10/10 på alle tre tiny-varianter (de falske SUSPECT'er på rene
   filer, jeg målte før, kom af, at VAD manglede i testen), men greedy mister 4 af 200 blok-celler (199 → 195), fordi VAD ikke kan
   bekræfte en blok på sparsom dialog.

**Målt effekt:**

| | Før | Efter |
|---|---|---|
| Svage ramper, tiny.en | 75/100 | 95/100 |
| Svage ramper, tiny.en-greedy | 71/100 | 96/100 |
| Svage ramper, tiny.en-q5_1 | 74/100 | 98/100 |
| Stor matrix (580), 5 modeller | uændret | uændret eller bedre (base.en 529 → 536, small.en 551 → 553, q5_1 524 → 526) |
| Blue Mountain State S01E10 | kun CPU bestod porten | GPU og CPU består begge |

Efter ændringerne fejler 11 af 300 svage-ramp-celler, alle med 0,26-0,42 s rest (under 1 s). Hele testsuiten: 532 bestået.

## 4. Tolerancer og grænser, der er ændret

| Konstant | Før | Efter | Fil |
|---|---|---|---|
| RATE_MIN_KEEP | 0,90 | 0,85 | verifyarr/subtitles.py |
| RATE_WEAK_MIN_TILT_S (ny) | | 0,85 s | verifyarr/subtitles.py |
| RATE_WEAK_MIN_RHO (ny) | | 0,40 | verifyarr/subtitles.py |
| RATE_WEAK_MIN_GAIN_S (ny) | | 0,07 s | verifyarr/subtitles.py |
| RATE_WEAK_MAX_RESID_S (ny) | | 0,32 s | verifyarr/subtitles.py |

Andre grænser er ikke ændret, heller ikke efter sweepet. RATE_MIN_RHO (0,70), RATE_MIN_TILT_S (1,5 s) og de øvrige RATE_*-grænser
står, fordi det svage trin tager de tilfælde, de afviste. Testens bestå-krav for svage ramper (p50 ≤ 0,25 s) er også uændret.

## 5. Testet og forkastet: alass som sidste trin

Idé: kør alass igen på filen, efter Whisper-rettelsen har lagt den på plads, og behold kun resultatet, hvis fejlen mod Whisper-
transskriptet falder. Risikoen for et forkert svar langt væk burde være væk, når filen allerede er tæt på.

Test: rigtig alass (kun en fast forskydning, ingen splits) med replay i `auto`-tilstand, 680 celler på tiny.en-greedy (100 svage
ramper og hele matrixen), accept hvis median-fejlen faldt ≥ 0,04 s og andelen over 1 s ikke steg. Resultat: **0 af 680 celler
ændrede sig**, hverken bestået/fejlet eller p50. alass flytter en rettet fil højst ca. 0,06 s. Resten (0,3-0,4 s) er Whisper-støj
og en lille rest af rampen, som en tale/stilhed-matchning ikke kan se. Koden er fjernet igen. Genprøv ikke.

## 6. Åbent

* **VAD-bekræftelsen af blokke** (`vad.shift_fits_speech`) sammenligner to scorer uden margin. Ved lighed afvises en ægte blok, men
  jitter-fejlalarmer giver også lighed. En margin ville bytte missede blokke mod falske alarmer. Ikke ændret.
* **Huller og cut_ends** detekteres kun og fejler på 15-36 af 40 cut_ends-celler uanset grænserne. Ikke en følge af skrøbelige
  grænser alene.
* **ANCHOR_MIN_OVERLAP** (6-12 celler flipper på de svage ramper) og ANCHOR_MIN_COUNT på den store matrix er ikke undersøgt videre.
* Den nye rate-port er kun målt i replay, ikke i en rigtig GPU-kørsel af hele pipelinen.
