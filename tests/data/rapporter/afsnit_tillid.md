# Afsnit-tillid: hvilke afsnit kan bruges som facit?

Metode: samme metrik som `verifyarr_handoff/oracle.py` (`global_offset`: bedste konstante
offset ved word-time agreement, matchvindue ±2,0 s, tokens ≥4 bogstaver). Reference er
`out/<slug>.words.json` (large-v3-turbo), undertekst er den `.en.srt`/`.en.hi.srt`-fil
hver fixture (`verifyarr/tests/fixtures/whisper_full/<slug>.json`, felt `subtitle_name`) peger på.
Sweep ±60 s (grov 0,25 s + fin 0,05 s omkring bedste). Residual/spredning: median-residual pr.
tredjedel af afsnittet ved bedste offset (`spread` = max−min), lineær drift-hældning (s/min)
samt agreement pr. tredjedel (`3. min`). Nabo-tjek og offset-sweep pr. tredjedel for afvigere.

Klasser: **FACIT** = facit-egnet · **INDHOLD** = rigtigt indhold, men ude af sync
(brugbar til indholdstest, ubrugelig som timing-facit) · **FORKERT** = forkert indhold.
`Gammel` = nuværende BAD-liste i `min_coverage.py` (BAD/GOOD). `⚠` = afviger fra den.

| Afsnit | Klasse | Gammel | Offset (s) | Agree bedst | Agree v0 | Spread (s) | Slope (s/min) | 3. min | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| C_S02E24 | FACIT | GOOD | +1.60 | 0.805 | 0.722 | 0.20 | +0.014 | 0.79 |  |
| C_S02E13 | FACIT | GOOD | +1.85 | 0.785 | 0.644 | 0.11 | +0.005 | 0.77 |  |
| C_S02E03 | FACIT | GOOD | +1.75 | 0.783 | 0.649 | 0.07 | +0.005 | 0.75 |  |
| C_S03E19 | FACIT | GOOD | +1.30 | 0.781 | 0.734 | 0.03 | -0.002 | 0.77 |  |
| C_S03E13 | FACIT | GOOD | +1.10 | 0.780 | 0.716 | 0.11 | +0.008 | 0.76 |  |
| C_S03E03 | FACIT | GOOD | +1.70 | 0.777 | 0.690 | 0.10 | +0.010 | 0.77 |  |
| C_S03E18 | FACIT | GOOD | +1.75 | 0.775 | 0.697 | 0.25 | +0.010 | 0.74 |  |
| C_S02E12 | FACIT | GOOD | +1.95 | 0.771 | 0.656 | 0.11 | -0.004 | 0.76 |  |
| C_S03E22 | FACIT | GOOD | +1.45 | 0.771 | 0.727 | 0.13 | +0.009 | 0.74 |  |
| C_S02E09 | FACIT | GOOD | +1.90 | 0.762 | 0.655 | 0.23 | -0.009 | 0.75 |  |
| C_S03E21 | FACIT | GOOD | +1.30 | 0.761 | 0.721 | 0.05 | -0.005 | 0.75 |  |
| C_S03E17 | FACIT | GOOD | +1.30 | 0.759 | 0.703 | 0.34 | +0.003 | 0.75 |  |
| SH_S01E02 | FACIT | GOOD | +1.90 | 0.759 | 0.674 | 0.10 | -0.001 | 0.75 |  |
| C_S03E06 | FACIT | GOOD | +2.10 | 0.756 | 0.618 | 0.10 | +0.002 | 0.73 |  |
| C_S03E09 | FACIT | GOOD | +1.65 | 0.753 | 0.622 | 0.06 | -0.003 | 0.72 |  |
| C_S03E15 | FACIT | GOOD | +1.45 | 0.751 | 0.719 | 0.06 | +0.005 | 0.73 |  |
| C_S02E02 | FACIT | GOOD | +1.65 | 0.749 | 0.625 | 0.03 | +0.001 | 0.72 |  |
| C_S03E12 | FACIT | GOOD | +1.70 | 0.748 | 0.604 | 0.14 | +0.007 | 0.72 |  |
| C_S02E10 | FACIT | GOOD | +1.75 | 0.748 | 0.634 | 0.06 | +0.001 | 0.70 |  |
| SH_S01E06 | FACIT | GOOD | +1.90 | 0.748 | 0.653 | 0.05 | -0.000 | 0.71 |  |
| SH_S01E04 | FACIT | GOOD | +1.90 | 0.745 | 0.668 | 0.20 | +0.002 | 0.72 |  |
| C_S02E18 | FACIT | GOOD | +1.20 | 0.744 | 0.722 | 0.07 | -0.001 | 0.73 |  |
| C_S02E07 | FACIT | GOOD | +1.70 | 0.732 | 0.632 | 0.15 | +0.000 | 0.73 |  |
| SH_S01E03 | FACIT | GOOD | +1.75 | 0.731 | 0.641 | 0.07 | +0.001 | 0.68 |  |
| SH_S01E01 | FACIT | GOOD | +1.80 | 0.731 | 0.633 | 0.11 | +0.002 | 0.69 |  |
| C_S03E08 | FACIT | GOOD | +1.40 | 0.728 | 0.658 | 0.19 | -0.008 | 0.72 |  |
| C_S02E05 | FACIT | GOOD | +1.95 | 0.727 | 0.600 | 0.22 | +0.001 | 0.70 |  |
| C_S02E01 | FACIT | GOOD | +1.85 | 0.725 | 0.593 | 0.16 | -0.010 | 0.69 |  |
| C_S03E10 | FACIT | GOOD | +1.50 | 0.720 | 0.670 | 0.26 | +0.015 | 0.62 |  |
| SH_S01E05 | FACIT | GOOD | +1.55 | 0.691 | 0.622 | 0.11 | +0.004 | 0.67 |  |
| C_S03E16 | INDHOLD | GOOD | +1.60 | 0.771 | 0.638 | 0.52 | -0.007 | 0.75 | ⚠ Stykvis knek: midterste tredjedel -0.82 mod -0.30/-0.40 i siderne (spread 0.52). |
| C_S02E11 | INDHOLD | GOOD | +1.90 | 0.760 | 0.590 | 0.76 | +0.051 | 0.75 | ⚠ Restdrift: tredjedelsmedianer -0.83/-0.45/-0.07 (monoton), slope +0.051 s/min. |
| C_S02E04 | INDHOLD | GOOD | +1.70 | 0.745 | 0.610 | 0.91 | -0.061 | 0.68 | ⚠ Restdrift: tredjedelsmedianer -0.15/-0.56/-1.06 (monoton), slope -0.061 s/min. |
| C_S02E06 | INDHOLD | GOOD | +1.00 | 0.711 | 0.669 | 0.75 | -0.050 | 0.68 | ⚠ Restdrift: tredjedelsmedianer -0.05/-0.39/-0.80 (monoton), slope -0.050 s/min. |
| C_S03E04 | INDHOLD | GOOD | +1.35 | 0.710 | 0.620 | 0.76 | -0.048 | 0.69 | ⚠ Restdrift: tredjedelsmedianer -0.05/-0.45/-0.81 (monoton), slope -0.048 s/min. |
| C_S03E01 | INDHOLD | BAD | +1.65 | 0.625 | 0.559 | 0.03 | -0.001 | 0.37 | ⚠ Delvis: T1/T2 matcher ved +1.5s (0.81/0.74), T0 matcher ingen steder (bedst 0.43). |
| C_S03E11 | INDHOLD | BAD | +1.60 | 0.600 | 0.561 | 0.23 | -0.015 | 0.28 | ⚠ Delvis: T0/T1 ved +1.5s (0.74/0.76), T2 matcher ingen steder (bedst 0.52). |
| C_S03E14 | INDHOLD | BAD | +1.90 | 0.547 | 0.437 | 0.39 | +0.000 | 0.07 | ⚠ Stykvis: T1/T2 ved +1.5/+2.0s (0.80/0.81), T0 ved +17s (0.74). |
| C_S03E07 | INDHOLD | BAD | +24.25 | 0.500 | 0.018 | 0.18 | +0.035 | 0.08 | ⚠ Stykvis: T0/T1 ved +24s (0.69/0.75), T2 ved +36.5s (0.73). |
| C_S03E05 | INDHOLD | BAD | -0.85 | 0.444 | 0.424 | 2.44 | +0.132 | 0.35 | ⚠ Lineaer drift +0.132 s/min (ca. 2-3 s over afsnittet), spread 2.44. |
| C_S03E02 | INDHOLD | BAD | +19.20 | 0.439 | 0.019 | 0.09 | +0.001 | 0.01 | ⚠ Stykvis: T0/T1 ved +19s (0.71/0.48), T2 ved -13.5s (0.74). |
| C_S02E21 | INDHOLD | BAD | +0.95 | 0.409 | 0.384 | 0.26 | -0.021 | 0.04 | ⚠ Stykvis: T0 matcher ved +1.0s (0.78), T2 matcher ved +41.5s (0.67). |
| C_S03E20 | INDHOLD | BAD | -25.00 | 0.395 | 0.272 | 1.35 | +0.005 | 0.01 | ⚠ Stykvis: T0/T1 ved -25s (0.49/0.63), T2 ved +1.5s (0.63). |
| C_S02E08 | FORKERT | BAD | -59.85 | 0.102 | 0.047 | 0.89 | -0.047 | 0.02 |  |
| C_S02E19 | FORKERT | BAD | -35.85 | 0.070 | 0.048 | 1.35 | -0.035 | 0.01 |  |
| C_S02E20 | FORKERT | BAD | -33.55 | 0.023 | 0.009 | 0.68 | +0.011 | 0.01 |  |
| C_S02E22 | FORKERT | BAD | +5.75 | 0.022 | 0.014 | 1.49 | -0.004 | 0.02 |  |
| C_S02E16 | FORKERT | BAD | -35.80 | 0.021 | 0.010 | 1.18 | +0.045 | 0.02 |  |
| C_S02E17 | FORKERT | BAD | +46.00 | 0.020 | 0.014 | 0.84 | +0.007 | 0.01 |  |
| C_S02E14 | FORKERT | BAD | -12.55 | 0.020 | 0.014 | 1.49 | -0.030 | 0.02 |  |
| C_S02E15 | FORKERT | BAD | +60.10 | 0.020 | 0.011 | 0.58 | +0.016 | 0.01 |  |
| C_S02E23 | FORKERT | BAD | -21.55 | 0.017 | 0.012 | 1.34 | +0.043 | 0.01 |  |

## Tærskler (sat efter fordelingen, kalibreret mod Slow Horses)

De 6 Slow Horses-afsnit (eneste brugerbekræftede) ligger: agree bedst 0,691–0,759,
offset +1,55…+1,90 s, agree v0 0,622–0,674, spread ≤0,20, |slope| ≤0,004, 3. min ≥0,667.
Metrikken bakker kalibreringen op: bekræftet indhold scorer samlet højt, så tærsklerne
sættes efter dem — ikke omvendt.

- **FORKERT** (agree bedst < 0,30): fordelingen er tom mellem 0,11 og 0,39. Alt under 0,11
  matcher heller ikke nabo-afsnittenes referencer (>0,11 ingen steder) — det er ikke
  ombyttede filer, men fremmed indhold. Nærmeste over grænsen er C_S03E20 (0,395) med
  dokumenterede delvise match — korrekt placeret over grænsen.
- **FACIT**: agree bedst ≥ 0,65 (tomt interval 0,625–0,691 over SH-minimum; samme bar som det
  gamle arbejde, så tallene kan sammenlignes), |offset| ≤ 3 s, fald bedst→v0 ≤ 0,20,
  spread ≤ 0,40 (sund bulk ≤ 0,34, næste 0,52), |slope| ≤ 0,025 (bulk ≤ 0,015, drift ≥ 0,048),
  3. min ≥ 0,60.
- Resten er **INDHOLD**: rigtigt indhold et eller flere steder i filen, men timingen holder ikke.

Systematisk bias: ALLE facit-egnede afsnit — inkl. Slow Horses — har bedste offset +1…+2 s
og tredjedelsmedianer omkring −0,4…−0,8 s. Det er en common-mode-bias mellem whisper-ordtider
og undertekst-timings, ikke en sync-fejl pr. fil. Konsekvens: brug facit-filerne som de ligger,
flyt dem ikke med −1,7 s; biasen går ud ved sammenligning på tværs af afsnit.

## Afvigelse fra den nuværende BAD-liste

BAD-listen erklærer 17 afsnit dårlige. Tallene siger:

- 9 er reelt forkerte (agree ≤ 0,10, intet nabo-match): C_S02E08, E14, E15, E16, E17, E19,
  E20, E22, E23. Her holder BAD-listen.
- 8 har overvejende rigtigt indhold og er fejlklassificeret som ubrugelige: C_S02E21, C_S03E01,
  C_S03E02, C_S03E05, C_S03E07, C_S03E11, C_S03E14, C_S03E20 — alle med delvise agreement
  på 0,44–0,78 ved stykvise offsets (drift, +19/+24/−25 s-skift eller enkelt defekt akt).
- Omvendt erklærer listen 5 afsnit gode, som tallene ikke bakker op som timing-facit:
  C_S02E04, C_S02E06, C_S02E11, C_S03E04 (restdrift ~0,8–0,9 s monotont over afsnittet) samt
  C_S03E16 (stykvis knek ~0,5 s i midterakten). Intet BAD-afsnit kvalificerer sig til FACIT.

## Konklusion: hvilke afsnit bør matricen bruge som facit?

Ja — der er rigeligt. 30 af 52 afsnit er facit-egnede (24 Community + alle 6 Slow Horses):

C_S02E01, E02, E03, E05, E07, E09, E10, E12, E13, E18, E24, C_S03E03, E06, E08, E09, E10,
E12, E13, E15, E17, E18, E19, E21, E22, SH_S01E01–E06.

Til 10 afsnit foreslås de 6 Slow Horses (brugerbekræftede) + 4 Community med højest margin,
fx C_S02E24 (0,805), C_S02E13 (0,785), C_S02E03 (0,783), C_S03E19 (0,781) — alle med spread
≤ 0,20 og 3. min ≥ 0,72. De 5 GOOD-men-ikke-facit-afsnit (E04, E06, E11, S03E04, S03E16)
bør udgå som timing-facit, indtil de måles mod en syncet udgave af underteksten (lineær
re-sync pr. afsnit fjerner driften; E16 kræver stykvis re-sync af midterakten).
C_S03E05 (drift +0,13 s/min) er mønstereksemplet på et afsnit, hvor recovery-målingen i dag
straffer pipelinen for at synke mod lyden: efter re-sync hører den hjemme i INDHOLD→FACIT.
Det svage drift-resultat kan dermed delvis være et facit-problem, ikke et pipeline-problem —
gentag drift-målingen på de 10 foreslåede, før der konkluderes om pipelinen.
