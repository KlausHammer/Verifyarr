# Jitter-recheck: svar

Kun måling. Ingen filer i verifyarr er ændret, og matricen er ikke kørt.
Genkørsel af Q3-sættet (357 rækker: 356 `run_one` + 1 missing-marker for
medium.en-q5_0/SH_S01E03, samme fixtures, sweep-transskripter og −45 s-shift
som baseline) mod den rettede kode i egen mappe `/tmp/jitter_recheck`.
Instrumenteret pr. række: screen/fps/jitter-trigger, sampled-jitter før
eskalering, endelig jitter, og jitter-dom via note-match ("Cue timing is
noisy"). Kørsel: `/tmp/jitter_recheck.py`, data i `/tmp/jitter_recheck.jsonl`
+ `/tmp/jitter_recheck_jitter.jsonl`, log i `/tmp/jitter_recheck.log`.

Konklusion: begge falske SUSPECT er væk (0 jitter-domme på 356 korrekte
rækker), prisen er 3 købte fulde transskripter på 178 sampled-rækker, og
alle 10 jitter-rækker dømmes stadig SUSPECT.

## 1. SUSPECT pr. model/mode (korrekte filer)

356 rækker, 2 SUSPECT totalt, **0 fra jitter-grenen**:

| model | full (n/SUSP/jitSUSP) | sampled (n/SUSP/jitSUSP) |
|---|---|---|
| turbo | 12 / 0 / 0 | 12 / 0 / 0 |
| small.en | 12 / 0 / 0 | 12 / 0 / 0 |
| tiny.en-greedy-cpu | 12 / 0 / 0 | 12 / 0 / 0 |
| tiny.en-cpu, tiny.en-q5_1-cpu | 12 / 0 / 0 | 12 / 0 / 0 |
| base.en-cpu/greedy/q5_1 | 12 / 0 / 0 | 12 / 0 / 0 |
| medium.en, medium.en-greedy | 12 / 0 / 0 | 12 / 0 / 0 |
| medium.en-q5_0 | 10 / **2** / 0 | 10 / 0 / 0 |
| small.en-greedy, small.en-q5_1 | 12 / 0 / 0 | 12 / 0 / 0 |
| turbo-q5_0, turbo-q8_0 | 12 / 0 / 0 | 12 / 0 / 0 |

De 2 resterende SUSPECT er det kendte pre-eksisterende fund
(medium.en-q5_0 `SH_S01E01` clean + uniform_neg full, anchor-eskalering
Δ−4,0/−4,5 s, jit 0,16–0,17) — uændret fra baseline, ikke jitter-relateret.

De to gamle falske jitter-SUSPECT:

- turbo `SH_S01E02` uniform_neg sampled: sampled-jit 0,65 (n=5) →
  jitter-trigger eskalerer → endelig jit **0,13** (n=65 fulde ankre) → **ok**.
- small.en `SH_S01E06` uniform_neg sampled: sampled-jit 0,565 (n=6) →
  eskalerer → endelig jit **0,165** (n=48) → **ok**.

Full-mode er uændret (maks endelig jit 0,295, som baseline).

## 2. Jitter-triggerens meromkostning pr. model

Sampled-rækker hvor jitter-triggeren (≥0,4 over ≥5 ankre) alene køber det
fulde transskript — dvs. screen/fps havde ikke fyret i forvejen:

| model | sampled n | jitter-eskaleringer |
|---|---|---|
| turbo | 12 | 1 (`SH_S01E02` uniform_neg, js 0,65 → final 0,13, ok) |
| small.en | 12 | 1 (`SH_S01E06` uniform_neg, js 0,565 → final 0,165, ok) |
| tiny.en-greedy-cpu | 12 | 1 (`SH_S01E01` uniform_neg, js 0,445 → final 0,25, ok) |
| øvrige 12 modeller | 12/10 | 0 |

I alt 3 af 178 sampled-rækker (1,7 %); alle 3 ender ok. `jitter_would` ==
`jitter_hit` overalt på dette sæt (ingen overlap med screen/fps).
Højeste ikke-eskalerede raske sampled-jit er 0,365
(tiny.en-greedy-cpu `SH_S01E04` uniform_neg) — 0,4-barren adskiller rent.
Til sammenligning eskalerer 9 andre sampled-rækker via de pre-eksisterende
screen/fps-triggere (1 screen, 8 fps, heraf 4 fps på turbo); alle ok.

## 3. Jitter fanges stadig i sampled

10/10 SUSPECT, 0 ok (matrix-seedet jitter, ±1–3 s/cue):

| model | slug | mode | flag | endelig jit | gren |
|---|---|---|---|---|---|
| tiny.en-greedy-cpu | C_S02E03 | sampled | SUSPECT | 0,56 (n=33) | **jitter** ("disagree by 0.56s") |
| tiny.en-greedy-cpu | C_S02E03 | full | SUSPECT | 0,56 (n=33) | **jitter** |
| turbo | SH_S01E02 | sampled | SUSPECT | 0,515 (n=32) | anchor-eskalering |
| turbo | SH_S01E02 | full | SUSPECT | 0,515 (n=32) | anchor-eskalering |
| turbo | SH_S01E06 | sampled | SUSPECT | 0,57 (n=5) | anchor-eskalering |
| turbo | SH_S01E06 | full | SUSPECT | 0,66 (n=23) | anchor-eskalering |
| small.en | SH_S01E02 | sampled | SUSPECT | 0,62 (n=27) | anchor-eskalering |
| small.en | SH_S01E02 | full | SUSPECT | 0,62 (n=27) | anchor-eskalering |
| small.en | SH_S01E06 | sampled | SUSPECT | 0,75 (n=7) | anchor-eskalering |
| small.en | SH_S01E06 | full | SUSPECT | 0,64 (n=25) | anchor-eskalering |

- `C_S02E03` sampled eskalerer (screen; sampled-jit 0,655) og dømmes af
  jitter-grenen på det fulde transskript — 0,56 gengiver baseline-tallet
  eksakt, begge modes.
- De 4 SH sampled-rækker (kravet: mindst to på turbo og small.en) fanges
  alle via den ældre anchor-gren, som ligger før jitter-grenen i
  kæden — samme fangstvej som før rettelsen. To af dem havde <5
  MAD-ankre før eskalering (ns=2/3, jitter-trigger `None`) og blev fanget
  via screen-eskalering + anchor-dom; endelig jit ligger 0,515–0,75.
