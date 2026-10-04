# Missing middle: detektion uden rettelse

Resultat: hullet detekteres nu 24/24 i matricen (før 0/24, lydløst `ok`),
uden at nogen anden celle ændrer dom eller fil. Ingen Community-afsnit
flagges. Arbejdet er ikke committet.

## 1. Hvad der er bygget

- Full-mode: et cue-hul (≥20 s mellem to cues) hvis fulde transskript
  indeholder ≥90 s tale OG ≥150 ord dømmes SUSPECT uden omskrivning.
  Note: "Subtitle has no lines for 304 s at 21:10-26:14 where the audio
  has 148 s of speech -- part of the episode is missing; fetch a fresh
  subtitle." (SH_S01E01, 300 s klippet ved 40 %).
- Sampled-mode: et cue-hul ≥120 s i underteksten (gratis, ingen Whisper)
  køber det fulde transskript via den eksisterende eskalering; full-reglen
  dømmer. Samme mønster som jitter: trigger køber, dom på fuld dækning.
- Placering: ny `elif` efter jitter-grenen, lige før den endelige ok-gren.
  Blok/drift/fps/content-domme ligger før og kan ikke overskrives; en fil
  der ellers ender `ok`, bliver SUSPECT. Ingen omskrivning.
- Harness: `missing_middle` scores nu som detektion
  (`detected = flag != ok OG urørt`, + summary-tal). `recovered` beholdes,
  så raske timings stadig måles (p50 = 0,0 på alle 24).

Filer: `verifyarr/correctness.py` (tærskler + `cue_gaps`/`gap_speech`/
`missing_middle_evidence`), `verifyarr/pipeline.py` (trigger
`_missing_middle_says_needs_full`, dom, `_missing_middle_hit` læser det
cachede transskript), `tests/e2e_matrix.py` (scoring), `tests/test_silent_rows.py`
(3 nye tests). Korte kommentarer overalt.

## 2. Tærskelbegrundelse (kun Slow Horses, produktionsfiltrerede transskripter)

Målt på alle 6 SH-afsnit × 15 modeller, tale-sekunder som overlap, ord fra
segmenter der starter i hullet:

| | rask max tale | rask max ord | mm min tale | mm min ord |
|---|---|---|---|---|
| tiny.en-greedy-cpu (matrixmodellen) | 46,0 s | 87 | 121,1 s | 330 |
| værste model (tale: turbo-q5_0, ord: base.en-q5_1) | 112,3 s (20 ord) | 133 (82,6 s) | 116,1 s | 330 |

90 s OG 150 ord adskiller rent på alle 15 modeller: intet rask hul
klarer begge barre samtidig; alle 6 injicerede 300 s-huller klarer begge
med margin på ordsiden (330 mod 150). Ærligt forbehold: talesiden alene
adskiller IKKE på tværs af modeller (rask 112,3 s mod mm-min 116,1 s —
en strækning med credits-musik og lange segment-tidsstempler); det er
ordkravet der bærer sikkerheden.

Afvigelse fra opgaven: transskriptet filtreres med `full_transcript_for_check`s
egne to filtre (nonspeech + repetitionsløkker), ikke kun nonspeech.
Begrundelse: ufiltreret hallucinerer turbo "And dance with the big boys
again." 86 gange i E01-hullet (112,4 s/653 ord — falsk positiv på en rask
fil); med produktionsfiltret er samme hul 28,0 s/65 ord. Reglen dømmer dermed
på præcis den evidens produktionen i forvejen bruger.

Raske SH-huller ≥120 s (triggerkandidater): E01 511-689 s (178 s),
E02 1652-1826 s (174 s). Ingen andre SH-afsnit har huller ≥120 s.

## 3. Meromkostning (sampled, SH-matricen)

70 af 276 sampled-rækker køber nu det fulde transskript uden at have gjort
det før: 12 er missing_middle-målet (alle SUSPECT), 58 er raske rækker der
ender `ok` (E01/E02s naturlige huller på tværs af scenarier + 4 dropdup-rækker
på E04/E06 hvor tabte cues smelter nabohuller sammen over 120 s). Pris i
alt: ~198.000 friske lydsekunder (≈55 timers transskription) fordelt på
70 rækker. Full-mode koster intet ekstra (transskriptet er der i forvejen).

## 4. Matrix (krav 6)

`sh_mm1` (SH-only, tiny.en-greedy-cpu, workers 14, `--fresh-db --redo`,
552 rækker) mod `tests/sh_jit2.jsonl`:

- missing_middle: 24/24 `detected` (SUSPECT + urørt, p50 0,0). Før: 24/24 `ok`.
- Alle 528 øvrige rækker: identisk flag, sync, recovered, untouched og
  lo_fixed. Ingen tabte rettelser, ingen nye flag.
- Eneste øvrige ændring er informativ: note-tekst og `lo_flagged`-tal på
  eskalerede E01/E02 sampled-rækker (fyldigere evidens giver anden
  unconfirmed line-order-flade; filen røres ikke, lo_fixed identisk).

## 5. Rigtige afsnit (krav 7)

`sh_mm1_genuine` (104 rækker) mod `tests/sh_jit2_genuine.jsonl`:

- SH: 12/12 `ok`, ingen omskrivning. 1 af 12 rækker ændrer informative
  felter: E02 sampled går `lo_flagged` 5→3 + ny note (eskaleringens fulde
  evidens bekræfter 1 swap som "reported, not repaired" og indsnævrer
  resten). Dom og fil er uændrede; triggeren virkede efter hensigten
  (købte, dømte `ok`).
- Community: 0 af 92 rækker ændrer sig. Ingen Community-afsnit flagges —
  der er intet for brugeren at tjekke.

## 6. Tværs af modeller (raske SH, run_one-sæt)

179 rækker (6 SH × 15 modeller × clean × full/sampled, audio on;
1 kendt skip: medium.en-q5_0/SH_S01E03 DTW-nedbrud):

- 0 missing-middle-domme på raske filer, alle modeller, begge modes.
- 30 gap-eskaleringer uden anden trigger (E01+E02 × 15 modeller):
  29 ender `ok`, 1 ender SUSPECT: medium.en-q5_0 SH_S01E01 sampled.
  Den er IKKE en missing-middle-dom (anden note, anden gren): eskaleringen
  blotlægger den kendte præeksisterende full-evidens-dom (3 ankre,
  Δ−4,0/−4,5 s — noten er byte-identisk med full-tvillingens, som var
  SUSPECT allerede ved baseline). Sampled er dermed konsistent med full.

## 7. Test og mutationer

- `tests/test_silent_rows.py::MissingMiddleTests` (3 tests, frisk DB):
  E01 missing_middle full + sampled → SUSPECT + urørt; E02 clean sampled
  → `ok` + urørt. Røde før (2 fejl), grønne efter.
- Mutationer (12/12 som ventet): barre op → mm-tests fejler; barre ned
  (1 s/1 ord) → kun E02-testen fejler; trigger slået fra → kun sampled
  mm-test fejler (full klarer sig uden trigger); dom slået fra → begge
  mm-tests fejler.
- Fuld suite: 301 tests, OK.

Rådata: `tests/sh_mm1.jsonl`, `tests/sh_mm1_summary.json`,
`tests/sh_mm1_genuine.jsonl`, `/tmp/mm_measure.py`, `/tmp/mm_models.py`,
`/tmp/mm_crossmodel.py` (+ `.jsonl`), `/tmp/mm_mutate.py`.
