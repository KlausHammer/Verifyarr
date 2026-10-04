# Jitter-review: svar

Kun review. Ingen filer i verifyarr er ændret, og matricen er ikke kørt.
Alle tal nedenfor er enten genregnet fra `tests/hx_jit.jsonl` (som indeholder
præcis de MAD-værdier reglen dømmer på — `anchor_map`s 4. felt) eller fra
målrettede `run_one`-kald (356 rækker: 6 SH-afsnit × 15 modeller × 2 modes ×
clean/uniform_neg), som ikke er matricen. Arbejdsfiler ligger i `/tmp`
(`jitter_q3*.py`, `jitter_q4.py`, `jitter_linelevel.py`).

Verificerede påstande fra opgaven: `hx_jit.jsonl` mod `hx_fps2.jsonl`
(1656 rækker, samme nøgler) giver præcis 4 flag-diffs, alle
`C_S02E03/jitter ok→SUSPECT` med noten "disagree by 0.56s".
`hx_jit_genuine.jsonl` er byte-identisk med `hx_fps2_genuine.jsonl` (`cmp`).
Min genberegning af median-MAD fra `anchor_map` rammer notens 0,56 på alle
4 celler, så metoden gengiver reglens input eksakt.

Konklusion: Q1 ja (to smalle, MAD-neutrale undtagelser). Q2 ja — MAD på
raske filer drives af fejlmatches, parafrase, dobbelt-tale og segmentering,
ikke kun cue-timing; men medianen over klip holder raske filer under 0,5 i
full mode. Q3 nej — marginen holder ikke på tværs af modeller: turbo og
small.en fyrer falsk SUSPECT på korrekte filer i sampled mode. Q4: ét
strukturelt hul (post-resync-ok tjekker ikke jitter, uobserveret i data) +
tre blinde vinkler by design (<5 ankre, MAD>1,0-klip udelukkes,
fremmedsprog) + bazarr-vetting uden reglen.

## 1. Hører `result["samples"]` til den endelige fil? Ja

`result` i jitter-grenen (`pipeline.py:2047`) har været igennem hele
beslutningskæden før den: ambiguous-opløsning (vinderens egen evaluering),
fps-fix (re-gather mod den rescalede fil) — og er aldrig passeret et
anchor-resync (den gren er gensidigt udelukkende med jitter-grenen: resync
ender i sin egen ok/SUSPECT-afgørelse, ellers SUSPECT). Cache-genbrug er
nøglet på undertekstens indhold *inkl. timings* (`subs_fingerprint`:
start|end|tekst) + whisper_mode + provider/model, så genbrugte samples
beskriver altid netop `current_subs`. Transskripter er pr. video og nøglet
(provider, model) — der findes ingen sti til en anden videos transskript,
og sprog-mismatch giver ingen ankre (`anchors_applicable`), altså
`anchor_jitter() is None` → ingen dom.

To undtagelser findes, begge uden praktisk betydning for jitter:

- `_synthetic`-fallback (`pipeline.py:1048-1054`): hvis vinderen old/blocks
  ikke kunne scores (`ev is None`), dømmes på `new`s samples mens
  `current_subs` er vinderens. Med ankere næsten uopnåelig: tom
  transskript-cache tvinger vinderen til `new` i forvejen, og den
  tilbageværende sti (under-tærskel, `Δ<min_change_seconds`) flytter cues
  så lidt at matchningen — og dermed MAD — er ~uændret. MAD er desuden
  invariant over for globale skift.
- `_recheck_after_resync`-fallback efter fps-fix: ved fejl/skip beskriver
  `result` pre-fix cues. Et fps-fix er en global stretch (0,1 % over et
  30 s-klip ≈ 30 ms), som ikke flytter MAD meningsfuldt — og en jitter-fil
  fyrer i øvrigt ikke fps-fixet (intet rent tilt i spredte ankre).

Jitter-grenen er ikke gatet på `anchor_check_enabled` — den virker også
med anchor-tjekket slukket. Der er ét produktions-kaldsted (linje
2047/2049).

## 2. Kan MAD stamme fra andet end cue-timing? Ja — verificeret på linjeniveau

Tre klip på clean, urørte, ok-dømte filer er regenereret eksakt (shift+MAD
matcher `hx_jit.jsonl` på øren) via `_match_segments_to_lines` med
tiny.en-greedy-cpu + fixture-undertekster:

- `SH_S01E03 @2023.7` full, MAD 1,00, 3 ankre: shifts −0,41, +0,59,
  **+32,04**. Det tredje er et rent fejlmach: segmentet "The men holding
  Hassan Ahmed is now less than four hours away" matches cue'en "of Hassan
  Ahmed." med overlap 1,00 — 32 s væk, inde i ±(30/60) s-vinduet. Én kort
  cue med et delt egennavn maksimerer klippets MAD.
- `SH_S01E01 @2967.6`, MAD 0,96, 6 ankre: shifts +0,80 … −1,20 ved median
  +0,03. Årsager: bandeord slettet i underteksten ("Fuck!" mangler),
  parafrase ("wanna"→"want to", "whiplash"-linjen), dobbelt-tale
  ("- What do you… - He can…") og tiny-fejlhøringer ("thinking rubbish",
  "Let's turn our office"). Filen er korrekt; uenigheden er tekst mod
  verbatim transskript.
- `C_S03E09 @573.8` full, MAD 1,00, 5 ankre: kondenserede/parafraserede
  cues mod verbatim Whisper + tæt dialog med tilordnings-ambiguïtet
  (hvilken cue hører segmentet til?).

Mekanismerne er altså: (a) falske linje↔segment-matches (korte cues, delte
navne/ord, overlap helt ned til 0,50 med kun 2 delte tokens),
(b) parafrase/kondensering i underteksten mod verbatim STT,
(c) dobbelt-tale/overlap, (d) STT-segmentering der deler sætninger andre
steder end cue-grænserne (se Q3-eksemplet), (e) tiny-fejlhøringer.
Cue-*varighed* indgår ikke i beregningen (kun start); de værste klips
cues er 1,2–4,7 s — ingen varighedseffekt observeret. Ombyttede linjer
påvirker ikke MAD: swap-scenariet topper på 0,26 (full) / 0,285 (sampled),
fordi matchningen er på event-niveau (samme tokens, samme event).
Sang/musik er ikke observeret som driver i de inspicerede klip, men
bemærk: `collect_samples`/`collect_samples_full` filtrerer ikke
nonspeech-segmenter (`[Music]` o.l.) før `clip_anchors` — det gør kun
evaluate/resync-stierne. En hallucineret sanglinje med 2+ delte tokens kan
derfor blive et falsk anker ad samme mekanisme som (a).

Omfanget på raske filer (flag ok, tiny.en-greedy-cpu, 1656 rækker):

- Pr. klip er støjen stor: 4764/46510 = 10,2 % af ankrede klip på ok-rækker
  har MAD ≥ 0,5 (clean+urørt: 238/2422 = 9,8 %). Det er medianen over klip,
  der redder raske filer — ikke rolige klip.
- Filet-maksimum på ok-rækker: full 0,32 (`C_S03E16` missing_middle),
  sampled 0,445 (`SH_S01E01` uniform_neg, n=12). Clean alene: 0,26/0,33.
  Per scenarie (ok, full): alle ≤0,27 undtagen missing_middle 0,32;
  (ok, sampled): uniform_p15 0,41, fps_late 0,375, resten ≤0,34.
- Note om kommentarens "healthy peak 0,39s (full)": de 0,39 stammer fra
  `C_S02E03` piecewise_c og `C_S02E09` cut_version — begge dømt SUSPECT ad
  anden vej. "Healthy" betyder her "uden injiceret jitter", ikke "ok-dømt".
  For faktisk ok-dømte filer er full-loftet 0,32, altså større margin end
  kommentaren antyder. Overvej at præcisere definitionen i kommentaren.

Ingen korrekt fil krydser 0,5 i full mode på disse data; i sampled mode er
0,445 tæt på — og Q3 viser at baren krydses på andre modeller.

## 3. Holder sampled-marginen på andre modeller? Nej

Målrettede `run_one`-kald (ingen matrix): 6 SH-filer × 15 modeller
(turbo-fixture + 14 sweep) × clean/uniform_neg × 2 modes = 356 rækker.
Metoden er valideret: for tiny.en-greedy-cpu gengiver den hx-tallene
eksakt (full-maks 0,26, sampled-maks 0,445 på `SH_S01E01` uniform_neg).

Rask-SH MAD-median, maks (p50) pr. model — clean + uniform_neg (fikset) samlet:

| model | full maks | full p50 | sampled maks | sampled p50 |
|---|---|---|---|---|
| turbo (fixture) | 0,295 | 0,185 | **0,650** | 0,217 |
| small.en | 0,165 | 0,130 | **0,565** | 0,135 |
| tiny.en-greedy-cpu | 0,260 | 0,198 | 0,445 | 0,260 |
| tiny.en-q5_1-cpu | 0,170 | 0,150 | 0,340 | 0,143 |
| tiny.en-cpu | 0,200 | 0,135 | 0,320 | 0,160 |
| turbo-q8_0 | 0,170 | 0,150 | 0,305 | 0,170 |
| base.en-cpu / greedy / q5_1 | 0,155–0,21 | ≤0,155 | 0,28–0,29 | ≤0,193 |
| medium.en / greedy / q5_0 | 0,165–0,22 | ≤0,15 | 0,21–0,275 | ≤0,14 |
| small.en-greedy / q5_1 | 0,18–0,19 | ≤0,15 | 0,23–0,25 | ≤0,15 |
| turbo-q5_0 | 0,200 | 0,170 | 0,225 | 0,193 |

"tiny er formentlig værst" er falsk for denne statistik: to **korrekte**
filer krydser 0,5 og dømmes SUSPECT af jitter-reglen alene:

- turbo `SH_S01E02` uniform_neg sampled: jit 0,65 (MADs
  [0,17, 0,48, 0,65, 0,68, 0,76], n=5), recovery p50=maks 0,025 s —
  perfekt fikset fil, falsk "Cue timing is noisy".
- small.en `SH_S01E06` uniform_neg sampled: jit 0,565 (MADs
  [0,07, 0,15, 0,55, 0,58, 0,89, 0,92], n=6), recovery 0,013 s — samme.

Uden reglen var begge dømt ok (notens ambiguous-verifikation holder
'new' med score ok; ingen anchor_bad — grenrækkefølgen afgør det).
Årsagen er small-n-medianer i sampled mode uden eskalering: 5–6 ankre,
hvoraf de to værste klip (378.5: MAD 0,68; 2047.4: MAD 0,76) er
heuristik-klynge-klip med ~7 s snævre anchor-vinduer og 3 ankre hver.
Værsteklippets 3 ankre er regenereret eksakt (+0,17, −1,49, −0,60 →
MAD 0,77): alle matches overlap 1,00 og korrekte, men turbo deler
sætningen "…heart attack the night / before, so I followed…" andre
steder end cue-grænserne — ren segmenteringsforskel på en fil med
0,025 s recovery-fejl.

Full mode er robust på tværs af modeller (maks 0,295, n=44 — dyb
dækning midler de 10 % støjende klip ud). Sampled uden eskalering er
det ikke: medianen over en håndfuld klip er ustabil, og 0,5-barens
afstand til 0,445 var målt på den forkerte model. Forvent flere
falske SUSPECTs når den fulde modelakse køres (her: kun SH,
kun clean/uniform_neg).

Sidefund (ikke jitter-relateret, pre-eksisterende): medium.en-q5_0
`SH_S01E01` clean og uniform_neg full dømmes SUSPECT via den gamle
anchor-eskalering (3 ankre Δ−4,0/−4,5 s ved 201/2907/2925 s; jit kun
0,16–0,17). Grenrækkefølgen (anchor_bad før jitter) garanterer at
jitter-ændringen ikke har forårsaget dem.

## 4. Stier hvor jitter stadig går lydløst

Række for række i `correctness_and_finish` — den eneste vej til ok uden
om jitter-grenen er **post-resync-ok**: efter vellykket anchor-resync
dømmes den korrigerede fil på flag/still-bad/slope-breaks/unproven-step/
run-offsets — aldrig på jitter. Hullet er strukturelt, men uobserveret:
kun 4 ok-rækker i hx-data gik gennem resync (`SH_S01E04` piecewise_b,
jit 0,24), og alle 5 jitter-rækker hvor resync'en fyrede
(`C_S02E02/04`, `C_S03E03/04`, `C_S03E09`-full) endte stadig SUSPECT
(14× "REMAINS", 4× "NOT verified"). Eget kombinationsforsøg (blok +12 s
på 2. halvdel + matrix-jitter, `C_S02E03`, tiny, begge modes) ender
SUSPECT via anchor-eskalering med jit 0,68/0,75 — ikke lydløst. At nå
lydløs-ok derigennem kræver kaskaden: anchor_bad tripper på en
jitter-fil + resync'en planner + recheck'en er ren + ingen pre-step
(`unproven_step` advarer ellers netop ved fravær af bevis). Muligt i
princippet (tynd sampled-dækning uden eskalering omkring en blokgrænse),
ikke set i 1656 rækker.

Blinde vinkler by design (delte med alle anchor-regler, men talfæstede):

- **<5 ankre → ingen dom**: 16 ok-rækker står på 4/16 ankre (alle
  sampled: `SH_S01E02/04/05/06` uniform*, `C_S03E09` uniform_p03/m5/
  missing_middle) — bærer de per-cue-støj, ser reglen det ikke. I
  sampled jitter-rækker uden eskalering: 6 rækker med 2–4 ankre
  (`C_S02E09`, `C_S03E09/16`), alle dog SUSPECT ad anden vej.
- **MAD>1,0-klip udelukkes** (`ANCHOR_MAX_MAD_SECONDS`): ekstrem jitter
  sletter sit eget bevis. Anchor-raten halveres af jitter: full
  0,77 (clean) → 0,38 (jitter); full jitter-rækker beholder dog ≥15
  ankre, så medianen overlever.
- **Fremmedsprog**: ingen ankre → `None`. Konstruktion, ikke hul.
- **Uden for rækken**: `bazarr.verify_subtitle_candidate` bruger
  standalone `correctness_check`, som ikke kender reglen — en jitteret
  erstatningskandidat accepteres på indhold og fanges først ved næste
  normale Scan. Forsinket, ikke lydløst.

Reglens recall er i øvrigt ikke 100 %: `SH_S01E02` (0,495) og `SH_S01E04`
(0,44) full jitter-rækker ligger under 0,5 — men begge var allerede
SUSPECT ad anden vej, så intet går lydløst af den grund. Samlet: 68/72
jitter-rækker fanges af ældre grene (50 eskalering, 14 REMAINS,
4 NOT-verified), 4 af den nye regel, 0 er stille.
