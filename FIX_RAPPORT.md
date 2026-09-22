# FIX_RAPPORT: fund 6.1/6.2 målt færdig

Dato: 2026-09-22. Kode under test: HEAD `2d44f6d` + arbejdstræets
`verifyarr/pipeline.py`. Model: `tiny.en-greedy-cpu`. Alle tal er målt gennem
rørledningen (`run_one`), aldrig offline ved siden af.

## 1. Dommen

Rettelsen virker, men den medbragte short-circuit (`if single_block: return
True`) er **fjernet igen efter ablation**: den bandt 10 nye stille rækker og
i alt 32 utilsigtede celleændringer, ingen tilsigtede. Uden den ændrer
rettelsen præcis de 18 tilsigtede celler af 1656 — 6× presync-skrivning (6.1)
og 12× urørt forkert-indhold (6.2) — og ellers ingenting. Whisper-omkostning
identisk før/efter (N100 målt, ikke antaget). Ægte filer: 8→2
forkort-indhold-omskrivninger (ikke 0 — se §5). Suiten er grøn.

## 2. Krav 1: mutationstest (målt, ikke antaget)

```
git stash push verifyarr/pipeline.py
.venv/bin/python -m pytest tests/test_fix_61_62.py -q   # 2 failed (4.4s)
git stash pop
.venv/bin/python -m pytest tests/test_fix_61_62.py -q   # 2 passed (3.1s)
```

På HEAD fejler begge som forudsagt: C_S03E10 uniform_neg melder "already in
sync" på en 45 s-forkert fil; C_S02E01 wrong_episode skriver `fixed (Δ44.4s)`
på forkert indhold. Efter rettelsen (også efter short-circuit-fjernelsen,
genkørt): `fixed (Δ44.8s, presync)`, rec 1.0 henholdsvis `left unchanged
(alass suggested Δ44.4s; no candidate matched...)` + SUSPECT. Begge tests
validerer rettelsen — ingen omskrivning nødvendig.

## 3. Krav 2+3: før/efter-matrice, celle for celle

`matrixdata/fix_before_*.jsonl` kunne IKKE genbruges: den dækker kun 4 af 8
holdout-afsnit, SH delvist (57–81 af 92 rækker/celle); kun 4 kendte
Community-afsnit er komplette. Begge arme er derfor kørt forfra, fuldt udsnit:
18 afsnit × 23 scenarier × 2 modes × 2 audio = 1656 rækker/arm, `--fresh-db
--redo --workers 14`. Før = `/tmp/vbase` (`git archive HEAD`, verificeret
byte-identisk med `git show HEAD:verifyarr/pipeline.py`); efter =
arbejdstræet. Begge arme 1656/1656 ok. (`.venv`-kopi unødvendig: launchere
bruger arbejdstræets venv; kun `verifyarr/`-importen skifter via
`VERIFYARR_UNDER_TEST`.)

Første efter-måling (MED short-circuit): **50 ændrede celler** — 32 for mange.
Alle er forklaret nedenfor; de er årsagen til fjernelsen i §4.

Endelig efter-måling (uden, `fix2_final` vs `fix2_before`): **præcis 18
ændrede celler, 0 uforklarede**:

| Celler | Skift | Mekanisme |
|---|---|---|
| uniform_neg full, C_S02E01/C_S02E12/C_S03E10 × on/off (6) | stille→rettet | 6.1-hovedgrenen: korrekt presync (+45 s) + forkastet alass-2-blok. Baseline skrives nu som `fixed (Δ.., presync)`, rec 0.000→1.000, i stedet for "already in sync" på diskens forkerte original |
| wrong_episode, C_S02E01/C_S02E04/C_S02E12 × full/sampled × on/off (12) | fixed→urørt, SUSPECT bevaret | 6.2-grenen: single-blok-deferral, intet indhold matcher → `left unchanged`, filen urørt. Flaggningen (100 %) var der før; kontrakten holder nu |

Ingen andre klasseskift på nogen af de 18 afsnit. `fix2_final` vs
`fix2_abl` (ablationskørslen): **0 ændrede celler** — bevis for at
`presync_desc`-fjernelsen (§7) er adfærdsneutral end-to-end.

### De 32 celler short-circuiten bandt (årsag til fjernelse)

Alle er single-blok-deferrals hvor "old" vandt uden ankerdækning:

- **6× rettet→stille** (C_S02E04/C_S02E12 fps_early sampled, C_S02E04
  uniform_p15 sampled; rec 1.0→0.84/0.0, flag ok): content-scoren er blind for
  små skift (vindue-overlap), så støjgab > 0.1-marginen lod old vinde outright
  uden om anker-sammenligningen. Målt på uniform_p15: old 0.8468 vs new ~0.75
  (grænse 0.7468 — afgjort med ~0.001), mens ankrene stod 0.7 s (new) mod
  2.2 s (old) over 13 klip hver og aldrig blev konsulteret.
- **4× advaret→stille** (C_S02E01 jitter × full/sampled × on/off):
  alass' Δ10.1s-fit på jitter er skrot (rec 0.0 begge arme — forkastelsen er
  korrekt), men flaget gik tabt: old evalueres ok og eskalerer ikke.
- **6× rec-værre, stadig flagget** (C_S02E02 piecewise_b/c 0.485/0.18→0.0,
  C_S03E16 piecewise 0.037→0.0): delvise alass-fixes forkastet.
- **2× stille→advaret** (C_S03E09 piecewise_c, rec 0.425→0.010, flag
  ok→SUSPECT): ærlig flagning i stedet for stille delfix — den eneste
  forbedring linjen bandt, til prisen af recovery-kollaps.
- **14× neutrale sti-skift** (rec 1.0 begge): presync-/framerate-/anker-veje
  i stedet for alass-direkte (bl.a. C_S02E12 fps_late via framerate-gren og
  uniform_m5 via anker-resync efter old-sejr — sikkerhedsnettet virker, anden
  vej til samme fix).

## 4. Den mistænkte linje (ablation)

Variant `/tmp/vabl` = rettelsen minus præcis de 3 linjer, fuld 1656-rækkers
matrice: **18 ændrede celler mod HEAD — kun de tilsigtede** (verdict-skift:
6 stille→rettet, 12 n/a→n/a). Linjen binder altså 0 tilsigtede og 32
utilsigtede celler, heraf 10 nye stille. **Fjernet.** Tilbage står én
kommentarlinje der begrunder fraværet med målingen. `single_block`-feltet og
-variablen er beholdt (notegrene + `apply_pending_sync` bruger dem).

Kendt tradeoff (analytisk, ikke målt — genuine blev ikke kørt med linjen):
uden linjen kan old kun vinde single-blok ved intet indholdsmatch eller
fravær af ankre; det koster C_S02E19-urørt på ægte filer (se §5). Med linjen
var den urørt, men prisen var 10 stille matrixrækker. Stille er den værste
klasse — fjernelsen står.

## 5. Krav 4: ægte filer

`.venv/bin/python /home/hammer/overfit/genuine.py` (104 rækker, arbejdstræets
kode) mod `radata/genuine.jsonl`:

| Klasse | Før (omskrevet / SUSPECT) | Efter |
|---|---|---|
| forkert-indhold (18) | 8 / 18 | **2** / 18 |
| facit-egnet (30×2) | 1 / 0 | 1 / 0 (samme SH_S01E04-full-FP, **0 nye**) |
| rigtigt-indhold-ude-af-sync (26) | 17 / 9 | 17 / 9 (identisk) |

6 rækker reddet (C_S02E14/C_S02E16 Δ1.1s, C_S02E17 Δ49.6s — alle nu `left
unchanged` + SUSPECT). Forventningen "8→0" holdt ikke: **C_S02E19 full+sampled
omskrives stadig (Δ39.6s), begge SUSPECT-flagget.** Mekanisme: old scorer
højest på indhold (full 0.60 vs 0.48; sampled 0.68 vs 0.30-SUSPECT) men
afvises uden ankerdækning (tomme ranges gør `_confirmed_in_every_block`
uopfyldelig), så new skrives; efterfølgende anker-eskalation flagger.
Detektion holder, urørt-kontrakten bryder fortsat her — ærligt negativt
resultat, ingen regression (rækken var omskrevet før også). Output gemt som
`tests/fix2_genuine_after.jsonl` (scriptets egen kopi ligger i /tmp og kan
forsvinde).

## 6. Krav 5: Whisper-omkostning (N100 målt)

Aggregeret over hele matricen, før vs efter (final):

- `fresh_audio_s`: 400049.1 s begge arme — **0 nye Whisper-kald**
- `cached_audio_s`: 3393278.2 s begge arme — ikke engang cache-genlæsning flyttede sig
- Celler med ændret `whisper_cost`: **0 af 1656**

"Tjekket kører i forvejen" er nu en måling: single-blok-deferral scorer 2
kandidater på allerede-betalte samples og koster præcis 0.

## 7. Tvivlspunkterne (HANDOFF §4, alle afgjort)

1. `max_shift_new`-binding: bundet via `.get()` i funktionshovedet (l.978) før
   al brug; hele suiten + 3×1656 matrixrækker eksekveret uden fejl. Lukket.
2. `_synthetic("old")` efter presync-skriv: beskriver disken. Bevis: grenen
   nås kun med winner="old" ∈ content_ok, så `scored["old"]` er baselinens
   egen evaluering, og baseline er netop det der skrives. Lukket.
3. Below-threshold-proxyen: **står som approximation** — 0 matrixceller tog
   `had_presync`-grenen (målt via note-signatur), så den er udækket af
   matricen; fejlen er øvre-begrænset af tærsklen (0.5 s). Ærligt åbent.
4. `sync_pair`-early-return flytter correctness-sampling ved presync: målt
   neutral — ingen diff-celle kan tilskrives den (alle presync-rækker uden for
   de 18 er celle-identiske). Lukket.
5. Short-circuiten: fjernet efter ablation (§4). Lukket.
6. Nye statusstrenge: alle forbrugere bruger `startswith("fixed")`
   (`db.py`, `jobs.py`, `reports.py`, frontend `StatusPill.tsx` → pill-ok);
   "left unchanged"-varianten falder i eksisterende muted-klasse som før.
   Ingen strikte parsere. Endelige rækker indeholder aldrig
   "[pending verification]" (deferral opløses altid før persist). Lukket.
7. Tomme `blocks_time_ranges`: tvinger indholdsgrenen pr. konstruktion
   (`_confirmed_in_every_block` returnerer False på tomme ranges →
   content-gren). Vist end-to-end: wrong_episode-noten scorer begge
   kandidater (new 0.06, old 0.07). Lukket.
8. `presync_desc`: **fjernet** (død værdi — noten bar allerede beskrivelsen
   via `presync_note`). Neutralitet bevist: final vs ablation 0 celler. Lukket.

## 8. Artefakter og status

- Endelig diff: `verifyarr/pipeline.py` (rettelse minus short-circuit minus
  `presync_desc`), `tests/test_fix_61_62.py` (urørt, begge består).
- Rådata (utracket, i repoet): `tests/fix2_before*.jsonl`,
  `tests/fix2_final*.jsonl`, `tests/fix2_abl*.jsonl`,
  `tests/fix2_genuine_after.jsonl`. `/tmp/vbase` (rent HEAD) og `/tmp/vabl`
  (ablation) eksisterer stadig, men /tmp kan forsvinde — jsonl-filerne er
  beviset.
- Suite: **212 passed, 44 subtests, 0 skipped** (4:25 min).
- **Ikke committet** — klar til Claudes gennemgang.
- Hvad der ikke lykkedes: genuine 8→2 i stedet for 8→0 (C_S02E19, begge
  flagget); below-threshold-proxyen er umålt på matrixdata (0 celler).
