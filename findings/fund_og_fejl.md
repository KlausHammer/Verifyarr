# Fund, fejl og forslag — løbende log

Skrives undervejs, ikke bagefter. Sidst opdateret 17. september 2026, kl. 22:30. Matricen er kørt færdig:
4200 rækker, 4172 ok, 28 skipped (én upstream-celle, se 3.1).

Formål: én samlet oversigt over hvad der gik galt, hvad vi fandt ud af, og hvad
vi foreslår ændret. Rettede fejl bliver stående — de er en del af historien.

---

## 1. Fejl i produktionskoden

### 1.1 Ugyldig UTF-8 fra lokal whisper væltede korrekthedstjekket
**Status:** rettet 17/9.

`correctness.py` læste whisper.cpp' output med
`json.loads(path.read_text(encoding="utf-8"))` og fangede
`(OSError, json.JSONDecodeError)`. `UnicodeDecodeError` er ingen af delene —
verificeret — så den slap uden om fejlhåndteringen og ville give en ubehandlet
exception.

Ikke teoretisk: whisper.cpp skriver reproducerbart en afkortet multibyte-sekvens.
Fundet i `base.en-greedy-cpu/SH_S01E05`, inde i en hallucineret sangtekst
(`" ♪ When it's poo-poo-poo\x8fa ♪"`). Transskriptionen blev kørt om og gav
**nøjagtig samme fejl** samme sted. Produktionens default for lokal whisper er
`small.en-q5_1` — samme modelfamilie, samme risiko.

**Rettelse:** `correctness.load_whisper_json()` læser bytes, falder tilbage til
erstatningstegn ved ugyldig UTF-8 og logger en advarsel. Malformet JSON fejler
stadig. Tre tests dækker det.

### 1.2 Dockerfile manglede `RUN` foran en betinget blok
**Status:** rettet 15/9 (fundet under kodegennemgang).
En `if [ -f ... ]; then ... fi`-blok stod uden `RUN`. Ugyldig Dockerfile-syntaks
der ville bryde `docker build`.

### 1.3 `.gitignore` udelod testdataen
**Status:** rettet 17/9.
Reglen `data/` matcher enhver mappe med det navn — også `tests/data/`. Hele den
nye testdata ville være blevet udeladt af git uden nogen fejlmeddelelse.
Rod-forankret til `/data/`.

---

## 2. Fejl i testopsætningen

### 2.1 Testen kørte ikke den opsætning der skibes
**Status:** rettet 17/9.
Matricen tvang `whisper_mode="full"` mens produktionens default er `sampled`, og
`line_order_audio_confirm=True` mens defaulten er `False`. Hele "collapsed
ladder"-designet (5 klip, eskalering) blev dermed aldrig rørt.

Konsekvens: det målte var *hvor godt et fuldt transskript er som grundlag*, ikke
*hvordan verifyarr opfører sig i drift*. Begge er nu akser i matricen.

### 2.2 Lydcachen var sat ud af kraft
**Status:** rettet 17/9.
`sync_pair` tager en `audio_cache`-dict. Matricen gav den en **tom dict pr.
række**, så cachen aldrig blev genbrugt. Hver række kørte ffmpeg på en 50
minutters .mkv fra Windows-drevet: ~100 s pr. række, CPU 88 % idle.

Værre: `whisper_gpu_staging/wav/` indeholdt allerede alle 60 afsnit som 16 kHz
mono PCM — præcis det format der blev udtrukket. 3,2 GB færdig lyd stod ubrugt.

**Rettelse:** cachen seedes fra `wav/`. **6 → 98 rækker/min.**

### 2.3 Kun 10 af 16 kerner kunne bruges
**Status:** rettet 17/9.
`e2e_matrix_parallel.py` shardede pr. afsnit — 10 shards, uanset `--workers`.
Dertil en lang hale: Community-afsnit er ~21 min, Slow Horses ~50, så de korte
shards blev færdige først og maskinen kørte til sidst med 6 aktive kerner.

**Rettelse:** shard på (afsnit × model) = 150 shards.
**CPU-udnyttelse 12 % → 83 %.** Fuld matrix: timer → 15 minutter.

### 2.4 Afsnitssættet var valgt før vi vidste hvilke afsnit der duede
**Status:** rettet 17/9.
Matricen kørte på et sæt valgt før tillidsmålingen. Tre af dem — C_S02E04,
C_S03E04, C_S03E16 — har målbar restdrift og duer ikke som facit.

Recovery måles mod afsnittets egne timings. Drifter afsnittet selv, straffes
pipelinen for at synke mod lyden. Sættet er nu ni brugerbekræftede og
måleverificerede afsnit plus C_S03E04 som dokumenteret drift-tilfælde.

### 2.5 Arbejdsmapperne blev aldrig ryddet
**Status:** rettet 17/9.
`tests/e2e_work_matrix_*/` samlede skrald på tværs af kørsler: 419 srt-filer hvor
én kørsel laver 72, plus efterladte WAV-udtræk (41–96 MB stykket). De er nu i
`.gitignore` og ryddes før kørsel.

### 2.5b Matricen var ikke et rent før/efter-instrument (17/9)
`tests/e2e_matrix.py:595`: `work.mkdir(exist_ok=True)`. Arbejdsmappens
per-(model,mode)-databaser overlever på tværs af kørsler, og `--redo` droppede kun
outputrækker, ikke cachen. Resultat: 105 af 4200 rækker ændrede sig mellem en før-
og efter-kørsel på kodestier ændringen umuligt kunne nå (anchor-resync med
Δ18–31 s, hvor A3-guarden kun rører |Δ| < 0,25 s).

Bevist: to sekventielle kørsler af samme shard med **identisk** kode gav 0
forskelle; samme shard i den parallelle matrix gav et tredje svar. Forskellen var
at testkørslerne fik friske shard-navne og dermed tom cache.

Mekanismen er værd at huske: en befolket DB får
`evaluate_against_cached_transcripts` til at finde **flere anchors**. Cachen
ændrer altså evidensen, ikke bare hastigheden. Invarianten for en shard-DB er
samme kode + samme scenariesæt + samme fixtures.

Rettet: `--redo` sletter nu DB-filerne for det den kører om. To huller står
tilbage og er ikke lukket: ændres scenariesættet, er gamle DB'er forurenede uden
at `--redo` opdager det; og en halvfærdig sweep-fil under én kørsel giver
legitimt andre input i den næste.

### 2.7 Matricens drift er 20× større end ægte drift (17/9)
`corrupt_drift` strækker `t → t*(1+0,02)`, altså ~1,2 s/min. De ægte drivende
afsnit ligger på 0,05–0,07 s/min (5.4). Matricen kan derfor vise om en
rate-korrektion **ødelægger** noget, men den kan ikke validere den på realistisk
drift — det kan kun målingen på de 21 afsnit.

### 2.6 `drift_swap` var aldrig blevet kørt
**Status:** rettet 17/9. Scenariet fandt straks noget, se forslag A4 — de to
rettelser modarbejder faktisk hinanden. Det havde vi ikke opdaget uden det.
Scenariet var fuldt implementeret — korruption, timing-scoring, swap-scoring —
men manglede i `DEFAULT_SCENARIOS`. Nu med, så fremtidige kørsler tager det
automatisk. 3600 → 4200 rækker.

---

## 3. Upstream-fejl i whisper.cpp

### 3.1 DTW-assertion crasher på lange filer
`medium.en-q5_0 / SH_S01E03`: `WHISPER_ASSERT: whisper.cpp:9076:
filter_width < a->ne[2]` efter ~170 s. Reproduceret to gange. `medium.en` og
`medium.en-greedy` klarer samme fil — det er kvantiseringen der rammer et
kanttilfælde. **Kan ikke omgås herfra.** 24 matrixrækker står som `skipped`.

**Forslag:** vælg ikke `medium.en-q5_0` til produktion uden at teste på lange
afsnit.

### 3.2 Ugyldig UTF-8 i output
Se 1.1. Omgået i både produktion og testharness.

**Bemærk:** begge upstream-fejl rammer kun Slow Horses-afsnit (~50 min mod
Community's ~21). To uafhængige fejl i samme retning — de lange filer presser
whisper.cpp hårdere.

---

## 4. Fund om datagrundlaget

### 4.1 Den maskinelle "gode afsnit"-liste holdt ikke
`min_coverage.BAD` var udledt af word-time agreement ≥ 0,65 og aldrig bekræftet
af et menneske. Målt på alle 52 afsnit (`afsnit_tillid.md`):

- **30 af 52** duer som facit
- **5 afsnit** listen kalder gode har målbar restdrift
- af de 17 udskudte er kun **9** reelt forkert indhold; 8 har rigtigt indhold med
  dokumenteret forskydning

Brugeren bekræftede selv 10 afsnit. **9 af dem består** målingen. C_S03E04 falder
igennem med −0,048 s/min ≈ 1 s over afsnittet — usynligt når man ser det, men for
meget til et sub-0,5s-facit. Begge vurderinger er rigtige; de svarer på
forskellige spørgsmål.

### 4.2 Det systematiske +1–2 s offset er ikke sync-fejl
To uafhængige målinger nåede samme konklusion: det er cue-start → ord-midt-lag.
Cues ligger hvor talen sætter ind (segmentstart mod cue-start: median −0,27…+0,21 s),
men første *indholdsbærende* ord kommer ~0,7 s inde, fordi korte ord filtreres af
`tokenize` (4+ bogstaver, stopord fjernet).

Det er undertekstningens normale forspring. **Anchor-mekanismen bevarer det af sig
selv**, fordi den måler fra segmentstart. Derfor fejlede ordniveau-anchors
(+0,66 s bias, andel uden for ±1 s steg 16 % → 27 %): de ville have skubbet
undertekster senere og ædt forspringet.

### 4.2b Forspringet er +0,15 s, ikke 0,7 s — 4.2's tal blev læst forkert (18/9)
Målt på 13 raske afsnit med produktionens egen matcher:

| måling | n | p10 | median | p90 |
|---|---|---|---|---|
| cue-start → **lydens** start (segment) | 2583 | −0,87 s | **+0,15 s** | +0,73 s |
| cue-start → første **indholdsbærende ord** | 5450 | +0,11 s | +0,69 s | +1,61 s |

Den nederste række reproducerer 4.2's "~0,7 s" eksakt. Men den måler afstanden
til første *lange* ord, efter `tokenize` har fjernet korte ord og stopord — en
artefakt af tokenizeren, ikke noget en seer oplever. Det tal er blevet citeret
som "undertekstens forspring" i brief og mål, og det er forkert.

Det rigtige forspring er **+0,15 s**: underteksten kommer et øjeblik før lyden,
i 62 % af tilfældene.

**Beslutning 18/9:** +0,15 s ER målet. Ingen global forskydning. Invarianten der
skal beskyttes er derfor *medianen af cue-start → lydens start = +0,15 s* — ikke
0,7 s. En rettelse der flytter den median er en regression, uanset hvad den
ellers forbedrer.

### 4.3 Sangmarkering er modelafhængig
| model | filer med ♪ |
|---|---|
| turbo (alle varianter) | 0 af 207 |
| small.en-q5_1, tiny.en, medium.en | 18 af 18 |

En ♪-baseret regel ville være virkningsløs på cloud-groq (default), men ikke på
lokal whisper, hvis default er `small.en-q5_1`.

---

## 5. Undersøgelser der endte i "byg det ikke"

Alle tre med tal bag. Negative resultater, men brugbare.

### 5.1 Baggrundstale
Findes (~4 % af indholdsbærende segmenter) og mekanismen står for 73 % af de
forkerte anchors. Men kun **3,6 %** af anchors er forkerte, og medianen i
`_robust_clip_shift` absorberer dem. Bedste signal (konfidens) har AUC ~0,70.
Alle filtre koster mere data end de giver.

### 5.2 Sangtekst
Omkvædsfrygten afkræftet: sang-dobbeltgængere har samme fejlprofil som andre.
Sang-anchors er dårligere (10,3 % mod 3,9 %) men bidrager kun 13 af 289 fejl. I
HI-undertekster er sang↔sang fejlfrit (30/30).

### 5.3 Klipgranularitet
E og F blev først målt på tredjedele af et afsnit (~80 anchors), mens
produktionen kører 5 klip à 60 s. Genmålt: konklusionerne holder. Klip har median
**14 anchors (min. 5)**, ikke 5–12 som anslået — klippene placeres i dialogtætte
partier. 147–148 af 150 klip konfidente.

Nyt fund som tredjedels-målingen skjulte: **2–4 af 150 klip har median lige over
1,0 s på filer der er i sync**. Værste udfald er en unødig ~1 s omskrivning — og
den ville æde forspringet (se 4.2).

VAD-placering søger **ikke** mod musik: 0 af 150 regioner.

### 5.4 Drift kan ikke ses ved produktionens parametre (17/9)
Forudsætningsmålingen til A1. Fuld rapport: `verifyarr_handoff/driftform.md`,
rådata `driftform.json` / `driftform_sampled.json`.

**Formen: begge findes.** Fire afsnit er ægte lineære (−0,05…−0,06 s/min ≈ 1 s
over afsnittet), ét er et ægte spring (C_S03E05, +3,21 s). Beviset for "lineær"
er ikke bare at det lineære fit vinder, men at springfittets falske cut opfører
sig som forudsagt på en rampe: hop ≈ halv totaldrift, placeret ca. midt i filen —
4 ud af 4 (forudsagt 0,67/0,62/0,55/0,64, målt 0,73/0,72/0,56/0,71).

**To afsnit er ikke ude af sync.** C_S03E20 og C_S02E21 har konstant-residual
0,16/0,19, *lavere* end de raskes median 0,23. Ingen model finder struktur.
Drift-sættet er n=4+1, ikke 7 — en uenighed med `afsnit_tillid.json`s
tredjedelsmål der står åben.

**Men ved 5 klip à 60 s findes signalet ikke.** Ingen af de fire størrelser
produktionen har adgang til adskiller:

| størrelse | raske | drivende | adskiller? |
|---|---|---|---|
| hældning, fuldt transskript | ≤ 0,018 s/min | ≥ 0,052 | ja, faktor 3 |
| hældning, anchors i klippene | ≤ 0,030 | ≥ 0,044 | nominelt, men samplingsfejlen er 0,016 median / 0,034 max — 2–5× marginen |
| hældning, de 5 klipmedianer | ≤ 0,035 | ≥ 0,035 | nej |
| klipmedian-spredning | 0,20–1,94 s | 0,34–1,30 s | nej |
| max abs klipmedian | 0,33–1,52 s | 0,28–1,24 s | nej |

Den raske C_S03E06 ligger over alle fire drivende på begge de nederste mål.

**Og det forklarer eskaleringen.** `_screen_says_needs_full` fyrer på 1 af 21
afsnit — blokafsnittet, via residual-reglen. På ingen af de fire ægte lineære.
Matricens 224/296 skyldes at dens drift er injiceret og stor. Spredningen er en
**blok**-detektor, ikke en drift-detektor; C_S03E05 får sampled-hældning +0,105
af et *spring*, så en rate-model må aldrig fyre på den.

**Det bevidste valg** (brief'en bad om det): byg ikke drift-detektion i sampled
mode; byg rate-korrektion dér hvor det fulde transskript allerede er betalt (nul
ekstra whisper-kald); accepter små ægte drifter, med Slow Horses' længde som
kendt blind vinkel (samme rate = 2,5 s over 50 min).

---

### 5.5 A4's præmis er forkert vej rundt — og punkt 4 kunne ikke køres (17/9)
Målt på matricens 4200 rækker, gennemsnitlig `recovered frac<=1,0s` og andelen
der når 0,90:

| scenarie | mode | rec≤1s | andel ≥0,90 | eskaleret |
|---|---|---|---|---|
| uniform | full/sampled | 1,000 | 1,000 | 0,000 |
| piecewise | full | 0,854 | 0,627 | 0,000 |
| piecewise | sampled | 0,684 | 0,530 | 0,776 |
| **drift** | **full** | **0,321** | **0,000** | 0,000 |
| **drift** | **sampled** | **0,059** | **0,000** | 0,731 |
| drift_swap | full | 0,321 | 0,000 | 0,000 |
| drift_swap | sampled | 0,062 | 0,000 | 0,619 |

**To ting følger.**

1. **Swappen skader ikke sync.** `drift` og `drift_swap` har identisk timing
   (0,321 mod 0,321 i full, 0,059 mod 0,062 i sampled). A4 blev formuleret som at
   "sync og linjeorden modarbejder hinanden"; skaden går kun den ene vej. Drift
   ødelægger swap-restore (0,400 → 0,167 i full, 0,149 → 0,000 i sampled), ikke
   omvendt.

2. **Og årsagen er triviel: drift bliver aldrig rettet.** Ikke én af 536
   drift-rækker når 0,90. Linjeordenen arbejder derfor altid mod forkert timede
   cues. Eskaleringen fyrer i 73 % af sampled-tilfældene og efterlader 0,059 —
   den køber et transskript ingen bruger.

**Punkt 4's eksperiment kunne ikke køres på eksisterende data.** "Swap-restore
efter *vellykket* drift-korrektion" kræver vellykkede drift-korrektioner, og der
findes nul. Det er A1 der skal skabe dem; eksperimentet kan først afgøres på en
matrixkørsel med A1 aktiv.

### 5.6 Linjeskift kan ikke afgøres på filniveau (18/9)
Punkt 3 bad om at måle rate-fordelingen før tærsklen blev sat, og at sige det
højt hvis der ingen dal var. Der er ingen dal.

`swap_severity` beregnes i dag kun når tærsklen allerede er slået ud, så
fordelingen fandtes ikke — kun halen (295 af 4200 rækker). `finalize_line_order`
returnerer nu (confirmed, checked, rate) på **hver** række, og matricen optager
det. Målt over 4200 rækker, swap+drift_swap mod clean+uniform+gap:

| mode | signal | fanger | falsk alarm på raske |
|---|---|---|---|
| full | rate | 96 % | **45 %** |
| full | antal bekræftede | 96 % | 46 % |
| sampled | rate | 65 % | **22 %** |
| sampled | antal bekræftede | 85 % | 62 % |

Det bedste punkt overhovedet kasserer hver femte raske undertekst. I full mode er
fordelingerne næsten identiske (swap p90 0,438 mod clean p90 0,418), fordi full
tester alle to-linjers events: 6 injicerede swaps i ~120 testede giver en rate der
drukner i detektorens egen falsk-bekræftelsesrate. I sampled er `checked`-medianen
**2** — en rate på 1,000 ud af to forsøg er ikke evidens.

Årsagen er B1/B4's: per-cue-detektoren bekræfter "byttet om" på masser af korrekte
linjer (krydsmodel-Jaccard 0,02–0,12), og file-level-raten arver støjen.

**Genmålt to gange siden, på ændret sampling** — netop fordi den oprindelige
konklusion hvilede på at sampled kun testede 2 kandidater i medianen:

| sampling | checked median | bedste tærskel, sampled | falsk alarm på raske |
|---|---|---|---|
| 5×60 (oprindelig) | 2 | 65 % fanget | 22 % |
| 8×20 | 3 | 100 % fanget | 50 % |
| 16×15 (endelig) | 3 | 99 % fanget | **57 %** |

Antallet af testede kandidater stiger ikke med flere klip, fordi det bestemmes af
heuristikken, ikke af samplingen. Ved den endelige konfiguration (n=140 swap mod
n=295 raske) ville triggeren kassere **over halvdelen af alle raske
undertekster**. Konklusionen står: der findes ingen brugbar tærskel, og
redownload-triggeren er bevidst ikke bygget.

**Konsekvens:** per-cue-reparationen er fjernet — den omskrev 40–61 uskyldige cues
pr. Community-fil på et grundlag med 2–12 % krydsmodel-enighed. Men
redownload-triggeren er **ikke** bygget: tallene bærer den ikke. Linjeorden er nu
rent rapporterende, og `apply_line_swap` er slettet frem for at stå som død sti.
`run_llm_confirm` → `compute_swap_severity` (den kaldte aldrig en LLM).

### 5.13 Punkt 4 kørt: linjeordenen kommer helt tilbage efter drift-korrektion (18/9)
Jeg afviste først eksperimentet med at det ikke kunne køres — matricen opnår
aldrig en vellykket drift-korrektion (0 af 536 rækker). Det var for hurtigt.
Korrektionen kan **konstrueres**: injicér drift + swap, fortryd driften eksakt
(t → t/1,02), og mål så. Tre tilstande pr. afsnit, 10 afsnit, sampled,
audio_confirm on:

| tilstand | linjeorden kørte | lo_flagged (middel) | SUSPECT |
|---|---|---|---|
| A: kun swap | 10/10 | 12,5 | 0 % |
| B: drift + swap | **2/10** | kørte ikke (de 2 der gjorde: 11,0) | 80 % |
| **C: drift + swap, drift fortrudt** | **10/10** | **12,5** | **0 %** |

*Rettet 18/9 efter Muses gennemlæsning: den tidligere "2,2" fremkom ved at
tælle `None` som 0 og modsagde afsnittets egen pointe — linjeorden kører ikke i B.*

**C er identisk med A.** Svaret på brief'ens spørgsmål er dermed ja: "drift
først, så linjeorden" er rigtigt, og kollapset er fuldstændig reversibelt. Der er
ingen varig skade som detektoren arver.

**Men mekanismen er en anden end antaget.** I tilstand B er `lo_flagged` ikke et
lavt tal — den er `None`. Linjeordenen **kører slet ikke**: filen bliver SUSPECT
først, og den gren kortslutter før `_apply_line_order` overhovedet konsulteres.
A4's kollaps fra 0,40 til 0,083 var altså ikke at detektionen blev dårligere af
støj; det var at den aldrig blev spurgt. Det er en mere præcis — og mere
beroligende — forklaring end "sync og linjeorden modarbejder hinanden".

Rådata: `punkt4.json`.

### 5.12 Det rigtige mål var "slipper den igennem uopdaget" (18/9)
Jeg optimerede længe efter SUSPECT-raten. Det er et mål for hvor ofte vi råber
op, ikke for hvor ofte vi fejler. Brugeren spurgte om 12 klip ville fange 99 % af
blokfejlene — det gjorde de ikke, de 99 % var drift — og spørgsmålet afslørede at
målestokken var forkert.

**En fil er kun sikker hvis den enten bliver rettet ELLER advaret om.**
Hverken-eller er en stille gennemgang: en undertekst der lander i biblioteket med
forkert timing uden at nogen siger noget. Det er den eneste fejl brugeren mærker.

Målt over 268 rækker pr. scenarie på alle 15 modeller, sampled:

| konfiguration | lydsek/fil | falsk SUSPECT | piecewise stille | drift stille | drift_swap stille |
|---|---|---|---|---|---|
| 5×60 (udgangspunkt) | 716,3 | 0/298 | 28 %±5 | 33 %±6 | 33 %±6 |
| 8×20 | 52,2 | 0/298 | 14 %±4 | 22 %±5 | 16 %±4 |
| 12×20 | 66,2 | 2/298 | 15 %±4 | 2 %±2 | 3 %±2 |
| **16×15 (valgt)** | **74,7** | 4/298 | **10 %±4** | **0 %** | **0 %** |

**Valget blev 16×15, og det omgjorde min egen forudlagte beslutningsregel.**
Reglen var "færrest stille gennemgange, forudsat nul falske alarmer", og den
pegede på 8×20. Reglen var forkert konstrueret: den vejede en linje i en rapport
lige så tungt som en undertekst med forkert timing der slipper igennem. En falsk
SUSPECT omskriver ingenting — `correctness_auto_action` er som standard "off".

To ting afgjorde det:
- **8×20 omskrev én rask fil i full mode** (turbo på SH_S01E06, anchor-resync
  Δ4,5 s, 2 rækker: audio_confirm on og off), altså præcis den grænse målet
  sætter. 16×15 rører ingen i nogen af de to modes. *Rettet 18/9: stod tidligere
  som "to raske filer"; de 18 øvrige ikke-urørte clean-rækker var SUSPECT-flag,
  ikke omskrivninger.*
- 16×15 er bedst på blokfejl OG lukker drift-hullet helt.

**Endelig validering, alle 15 modeller, skibbede defaults:**

| | før | efter |
|---|---|---|
| friske lydsekunder pr. fil | 716,3 | **74,7** (−90 %) |
| eskaleringsrate | 28,8 % | 0 % |
| raske filer urørt | 596/596 | **596/596** |
| rettelser 0 < \|Δ\| ≤ 0,24 s | 0 | 0 |
| piecewise recovery | 0,682 | **0,742** |
| ok-rækker uden gyldigt flag | — | 0 |

**Forbehold:** blokfejl har stadig det største hul — 10 % slipper igennem, og kun
55 % bliver rettet. Flere klip hjalp kun fra 28 til 10 %, så resten kræver en
anden mekanisme end sampling. Og de 4 falske SUSPECT pr. 298 rene filer bliver
til rigtige genhentninger hvis nogen sætter `correctness_auto_action` til
remediate; det bør måles igen før man gør det.

### 5.11 Marginalomkostningen ved genkørsel: 8 % (18/9)
Brief'ens punkt 2 bad om "lydsekunder første gang + marginal ved genkørsel".
Målt ved at køre samme video to gange mod samme database (14 rækker, 7 scenarier
× 2 audio_confirm, sampled, eskalering fra):

| | friske lydsekunder | fra cache |
|---|---|---|
| første kørsel, tom cache | **1613** | 720 |
| genkørsel, samme video | **135** | 60 |

Altså **8 %**. Pr. række svarer det til ~115 s første gang mod ~10 s ved
genkørsel. Det bekræfter præmissen om at rene lydsekunder overvurderer prisen:
et bibliotek der tjekkes igen, betaler næsten intet.

De 135 sekunder der bliver tilbage er de heuristiske linjeordens-klip, som
placeres ved kandidaterne og aldrig cachelagres (de er specifikke for den
enkelte underteksts timing). Da linjeorden nu er rent rapporterende (5.6), er
det den eneste whisper-udgift tilbage der ikke driver en beslutning — næste
oplagte besparelse, hvis den skal jages længere.

### 5.10 Eskaleringen afskaffet: 93 % færre whisper-sekunder (18/9)
5.8 sænkede forbruget 30 % ved at stramme eskaleringstærsklen. Det var for lidt,
og for tamt. Det rigtige spørgsmål var hvad eskaleringen egentlig *køber*.

**Svaret: den køber ikke information, den køber forsøg.** SUSPECT-reglen kræver
3 dårlige anchors i ABSOLUTTE tal (`ANCHOR_SUSPECT_MIN_SAMPLES`). Med 5 sampled
klip er det et 60 %-flertal; med det fulde transskripts ~21 vinduer er det 14 %.
De ~1300 lydsekunder gik altså til at gøre den samme tærskel lettere at ramme.

Gjort til en indstilling (`sync.anchor_suspect_min_samples`) og målt, 700 rækker
pr. politik:

| politik | lydsek/fil | eskalering | falsk SUSPECT clean | drift | drift_swap | piecewise |
|---|---|---|---|---|---|---|
| eskalering + 3 (hidtil) | 517 | 19,9 % | 0,0 % | 69 % | 68 % | 17 % |
| ingen eskalering + 3 | 52 | 0 % | 0,0 % | 45 % | 61 % | 20 % |
| **ingen eskalering + 2 (valgt)** | **52** | **0 %** | **0,0 %** | 62 % | **71 %** | **24 %** |
| eskalering + 2 | 517 | 19,9 % | 0,0 % | 73 % | 72 % | 21 % |

Samlet detektion over de tre ødelagte scenarier er *bedre* uden eskalering
(52,3 % mod 51,3 %) til en tiendedel af prisen. Recovery 0,631 → 0,629.

**Tærskel 2 er efterprøvet på ægte data, ikke kun matricen.** Dokumentationen
begrundede de 3 med "every false escalation (5 files) had 1-2 flagged samples".
Målt på 14 ægte raske afsnit ved produktionens 5 klip har **ingen af dem så meget
som ét** klip over 2,5 s — værste er 1,52 s. Den gamle måling må stamme fra en
anden granularitet.

**Multi-blok-gaten beholdt som billig option.** alass' eget blok-signal fyrer på
75 % af piecewise og 0 % af clean/gap/swap/uniform, gratis. Slår man eskaleringen
til, gælder den nu kun multi-blok-filer: 528 lydsek/fil i stedet for 766, og
piecewise-recovery genoprettet til 0,719 mod 0,669. Det koster 10× for 0,05 på ét
scenarie, så den er slået fra som default.

**Endelig måling, tiny.en-greedy-cpu, hele matricen:**

| | før | efter |
|---|---|---|
| friske lydsekunder pr. fil | 766 | **52** (−93 %) |
| eskalering | 31,4 % | 0 % |
| clean urørt | 40/40 | 40/40 |
| falsk SUSPECT | 0 | 0 |
| piecewise recovery | 0,719 | 0,669 |
| drift-detektion | 75 % | 65 % |

**To fejl i mine egne målinger undervejs.** (1) Matricen pinnede
`escalate_sampled_to_full=True` og `escalate_min_bad_samples=1` i sin egen config,
så to på hinanden følgende "efter"-kørsler viste nul effekt af ændrede defaults.
Fanget kun fordi jeg sammenlignede mod udgangspunktet. Der er nu en test der
binder matricen til `SETTING_DEFS`, så den ikke kan drive igen. (2) En
signaturændring brød matricens `spy_screen`-wrapper; den brede `except Exception`
slugte fejlen og gav 280 rækker med `status=ok, flag=skipped`, som jeg var tæt på
at rapportere. Verifikationsscriptet advarer nu hvis flag ikke er ok/SUSPECT.

### 5.8 Eskaleringspolitikken optimeret — 30 % færre whisper-sekunder (18/9)
Først målte vi bare at eskaleringen var dyr (5.7). Det er ikke et resultat. Så blev
reglen lavet om til en indstilling (`sync.escalate_min_bad_samples`) og fem
politikker kørt mod hinanden: 5 modeller × 10 afsnit × 7 scenarier × sampled ×
begge audio_confirm = 700 rækker pr. politik.

| politik | eskalering | friske lydsek/fil | rec≤1s | andel ≥0,9 | SUSPECT |
|---|---|---|---|---|---|
| 1 dårligt klip (hidtil) | 29,6 % | **739** | 0,633 | 0,591 | 22,6 % |
| **2 dårlige klip (valgt)** | **19,9 %** | **517** | 0,631 | 0,589 | 22,0 % |
| 3 dårlige klip | 19,9 % | 517 | 0,631 | 0,589 | 22,0 % |
| kun blok-spredning | 19,9 % | 517 | 0,631 | 0,589 | 22,0 % |
| slået helt fra | 0 % | **52** | 0,629 | 0,585 | 18,0 % |

**Valgt: 2.** −30 % whisper-lyd for −0,001 recovery. At min2, min3 og
"kun blok-spredning" giver *identiske* tal betyder at enkelt-klip-reglen aldrig
fyrer når man kræver to — alle resterende eskaleringer er spredningsdrevne, altså
netop de blokfiler `_try_anchor_resync` faktisk kan rette.

**Hvorfor ikke slå den helt fra**, når det sparer 93 %? Fordi dens værdi er
detektion, ikke reparation:

| scenarie | SUSPECT nu | min2 | slået fra |
|---|---|---|---|
| clean | 0,0 % | 0,0 % | 0,0 % |
| drift | 70 % | 69 % | **45 %** |
| drift_swap | 70 % | 68 % | 61 % |

Nul falske alarmer på raske filer i alle politikker. Men uden eskalering falder
opdagelsen af drift fra 70 til 45 %, og de filer ville så blive udgivet med
forkert timing uden at nogen opdagede det. Eskaleringen retter ikke drift — den
**ser** den.

### 5.9 Der findes intet mellemtrin i eskaleringsstigen (18/9)
Koden begrunder springet fra 5 klip til fuldt transskript med "escalating with
more probes plans correctly on 4 of 9 real files; escalating to the full
transcript reaches 8 of 9". Testet på 9 ægte fejlsynkede afsnit, med jævnt
fordelte 60 s-vinduer:

**Otte af de ni får ingen plan ud af det fulde transskript overhovedet.** Kun
C_S03E01 planlægger, og den kræver ~20 af 22 vinduer — 15 giver en *anden* plan,
12 og derunder ingen. Der er altså ikke et mellemtrin at lande på; for de otte er
eskaleringen ren omkostning uanset hvor mange klip man bruger.

Det gør 5.8's konklusion skarpere: eskaleringen skal beholdes for detektionen,
ikke for planlægningen, og derfor skal den fyre så sjældent som muligt.

### 5.7 Eskaleringen koster 10× — nu med tal (18/9)
Punkt 2 bad om lydsekunder med cachen indregnet. `correctness.WhisperCost` tæller
nu friske mod cache-serverede lydsekunder pr. fil, og tallet står i rapportrækken
og i matricens output. Målt på C_S03E10, sampled:

| tilfælde | friske lydsekunder | fra cache |
|---|---|---|
| ingen eskalering (clean/uniform/swap) | 107–131 s | 0–180 s |
| **eskalering (drift/piecewise/drift_swap)** | **1308–1580 s** | 60 s |

Eskaleringen springer fra ~5 klip à 60 s til hele afsnittet — en faktor 10. Det er
afgiften brief'en beskrev, nu målt. Bemærk at tallet tælles i
`collect_samples_full`, ikke i `generate`, netop så det forbliver ærligt under en
testharness der erstatter `full_transcript_for_check`.

## 6. Forslag til ændringer

### A. Kodeændringer
| # | hvad | grundlag |
|---|---|---|
| A1 | **Rate-korrektion — men kun på fuldt transskript.** Forudsætningsmålingen (5.4) viser at signalet ikke findes i sampled mode | sampled ≤1 s = 0,055 mod full 0,32. Eskalering fyrer (224/296) men redder ikke (0,047 efter), fordi den køber et transskript ingen bruger til et rate-fit |
| A2 | **Rekalibrér `_judge_order`** | restore 0,40 full / 0,15 sampled, men 21 uskyldige cues omskrevet pr. kørsel; krydsmodel-Jaccard kun 0,02–0,12 |
| A3 | **RETTET 17/9.** `_below_min_change()` lukker de to ubevogtede skrivesteder (`apply_pending_sync` og en fælles guard før begge vinderforgreninger i `_resolve_ambiguous_sync`). Det femte kald i `_try_anchor_resync` er dækket af den strengere `ANCHOR_RESYNC_MIN_SHIFT_S` = 1,0 s. Oprindelig beskrivelse: | tærsklen tjekkes kun ét sted (`pipeline.py:397`). Den tvetydigheds-løsende sti kalder `_write_fix()` uden at spørge igen: 240 rækker fik en rettelse på **0,1–0,2 s**, alle i `gap`. Filen skrives om for en forskel ingen kan se — og en rettelse på +0,1 s spiser af undertekstens forspring (se 4.2) |
| A4 | **Sync og linjeorden modarbejder hinanden ved drift** | `drift_swap`: timing upåvirket (0,321 full / 0,060 sampled, som drift alene), men **swap-restore kollapser fra 0,400 til 0,167 i full og fra 0,149 til 0,003 i sampled** (audio_confirm on; de tidligere 0,083/0,000 blandede on- og off-rækker, og off reparerer aldrig pr. konstruktion). Sync kører først; kan den ikke rette driften, ødelægger restfejlen lydbekræftelsen som linjeorden bygger på |

### B. Opsætning og flow
| # | hvad | grundlag |
|---|---|---|
| B1 | **Behold `line_order_audio_confirm = False`** | med `on` omskrives 40–61 model-divergerende cues pr. Community-fil lydløst |
| B2 | **Modelvalg: `small.en-greedy` (GPU, 44 s) eller `tiny.en-greedy-cpu` (CPU, 102 s)** | alle 15 modeller ligger 0,77–0,82 — når de er så tætte, vinder den hurtigste |
| B3 | **Overvej asymmetriske sync-tærskler** | `min_change_seconds` (0,25 s) og `ANCHOR_RESYNC_MIN_SHIFT_S` (1,0 s) bruger absolutværdi. −0,4 s øger forspringet (harmløst), +0,4 s spiser det. **Ikke målt endnu** |
| B4 | **Undersøg Community vs. Slow Horses** | Community får 40–61 linjeordensrettelser pr. fil, Slow Horses 0–2. Samme kode. Ægte kvalitetsforskel eller noget ved HI-undertekster? |

### C. Ikke et problem
- **timing på raske filer røres aldrig**: 596 af 596 `clean`-kørsler ender som
  "already in sync". Tærsklen på 0,25 s gør sit arbejde i den primære sti
- `uniform` og `gap`: 1,00 i begge modes
- eskaleringen fyrer aldrig forkert: 0 af 1184
- alass foreslår nul skift på 592 sunde filer — forspringet er ikke i fare
- baggrundstale, sangtekst, klipgranularitet: se afsnit 5

---

## 7. Metodiske fejl i vores eget arbejde

Med fordi de forklarer hvorfor flere konklusioner måtte laves om.

### 7.1 Konklusioner målt under forkerte forudsætninger — to gange
1. matricen kørte `full` mod produktionens `sampled` (2.1)
2. anchor-undersøgelserne målte på tredjedele mod produktionens 5×60 s klip (5.3)

Samme mønster: den fulde transskription er nem at måle på, så den bliver
målestokken — også når det ikke er den der kører. Begge blev fanget, men først
efter konklusionen var skrevet.

**Lære:** skriv produktionens faktiske parametre ned *før* målingen designes.

### 7.2 Eksisterende data blev ikke brugt
De 3,2 GB færdige wav-filer stod ubrugte mens testen genudtrak den samme lyd
3600 gange (2.2). Mappen var set i den allerførste mappeliste. Årsagen var at
den aldrig blev skrevet ind i opgavebeskrivelsen til den agent der byggede
harnessen.

**Lære:** enhver opgavebeskrivelse der rører data skal have en tabel med rigtige
stier øverst, og der skal tjekkes for eksisterende cache-mekanismer før noget
genberegnes.

### 7.3 Antagelser der blev afkræftet af måling
| antagelse | virkelighed |
|---|---|
| korte linjer ("Yeah, right.") kan give falske anchors | giver **nul** tokens — `WORD_RE` kræver 4+ bogstaver og stopord fjernes |
| ordniveau-anchors ville være mere præcise | gjorde det **værre** (+0,66 s bias) |
| ~5–12 anchors pr. 60 s klip | median **14** |
| arbejdsmapperne fyldes af srt-filer | 102 MB **wav**, 20 MB srt |
| langsom kørsel skyldtes for få tråde | ffmpeg; CPU var 88 % **idle** |

**Lære:** når CPU er idle under en "tung" kørsel, er trådantallet ikke
flaskehalsen. Mål hvad der faktisk bruger tid, før der konkluderes.

---

### 5.14 Full mode er blevet dårligere af ændringerne (18/9)
Fundet ved Muses gennemlæsning af den samlede rapport, efterprøvet af mig mod
`final.jsonl` mod `e2e_endelig.jsonl`. Hele optimeringen er målt i **sampled**,
som er produktionens default. Full mode blev aldrig set efter.

| måling, full mode | før (5×60) | efter (16×15) |
|---|---|---|
| drift recovery | 0,321 | **0,119** |
| drift_swap recovery | 0,321 | **0,119** |
| piecewise recovery | 0,854 | **0,802** |
| falske SUSPECT på raske rækker | 0/1192 | **76/1192** |

På den valgte model alene er tabet størst: `tiny.en-greedy-cpu` i full mode går fra
**0,471 til 0,093** i drift-recovery og fra 0,906 til 0,822 på piecewise — men med
**0 falske SUSPECT** i begge modes. Alle 76 falske advarsler kommer fra andre
modeller (small.en 8, turbo 6, base.en-q5_1 4, m.fl.).

**Årsagen er ikke fundet.** Sampling-parametrene burde ikke kunne påvirke full mode,
så det peger på `min_change_seconds` 0,25 → 0,5 eller `anchor_suspect_min_samples`
3 → 2 — den sidste er en absolut tærskel anvendt på ~21 vinduer i stedet for 16 klip.
Skal undersøges før nogen slår `sync.whisper_mode = full` til.

**Og en præcisering af "596/596 urørt".** Matricens `untouched` kræver *både*
"already in sync" *og* flag ok. Efter ændringerne er det 572/596 — men alle 596
clean-rækker har stadig `sync = "already in sync"`. De 24 er altså 24 nye
**advarsler** (4 sampled, 20 full), ikke 24 ændrede filer. Grænsen "ingen rettelser
på raske filer" holder; formuleringen "596/596 urørt" i commit 3154351 gør ikke.

### 5.15 Hvad der ikke kan efterprøves længere (18/9)
Ærlighedsnote, fundet ved samme gennemlæsning:

- **12×20-rækken** i 5.12's tabel: sweep-filerne er slettet under oprydning.
  5×60, 8×20 og 16×15 reproducerer eksakt fra gemte kørsler; 12×20 kan ikke.
- **Linjeordens-genmålingerne** ved 8×20 og 16×15 (50 % / 57 % falsk alarm): samme
  årsag. 5.6's oprindelige 4200-rækkers måling står.
- **A1's matrixkørsel** ("clean 596 → 577, eskalering +60 %"): koden blev aldrig
  committet, og kørslen findes ikke. Det der kan efterprøves i dag er
  `drift_effekt.json` (1 af 4 ægte afsnit forbedret) og hul-argumentet
  (støj 0,035 mod drift fra 0,044).
- **528 lydsek/fil** for multi-blok-gaten i 5.10: ingen gemt rådatafil.

### 5.16 Rettelser i denne log efter gennemlæsning (18/9)
- 5.13's tabel sagde `lo_flagged` = 2,2 for tilstand B. Det var `None` talt som 0 på
  8 af 10 afsnit og modsagde afsnittets egen pointe. Nu: "kørte ikke (2/10)".
- 5.12 sagde "8×20 omskrev to raske filer". Det var **én** fil (SH_S01E06, turbo) i
  2 rækker. De øvrige 18 ikke-urørte clean-rækker var SUSPECT-flag.
- §6 A4 sagde "0,40 → 0,083 i full". Det blandede audio_confirm on og off; med on er
  det 0,400 → 0,167. 5.5 havde 0,167 rigtigt hele tiden.
- Stadig forældet i denne log: 3.1 siger "24 skipped" (nu 28), og §6's B2
  (`small.en-greedy`) samt afsnit C beskriver tilstanden før disse fire commits.

---

## 8. Gennemgang af whispersub (18/9)

Kilde: https://github.com/ahernandezmiro/whispersub. Projektet genererer en
undertekst med Whisper og fletter den med en eksisterende fil — det matcher
cue-liste mod cue-liste, hvor vi matcher cues mod lyd. Men `src/alignment.py`
løser vores delproblem: hvornår tør man tro på et offset/en drift estimeret fra
støjende parvise matches. Fire kandidater blev vurderet. **Tre er målt ud, én er
implementeret.**

### 8.1 Vi gjorde allerede tre af deres ting
- **Ingen previous-text-conditioning.** De sætter `condition_on_previous_text=False`.
  Vi kører allerede `-mc 0` (max-context 0) i `_run_local_whisper`. Samme effekt.
- **Robust estimator.** De bruger least-squares med en klemme på skalaen
  [0,98; 1,02]. Vi bruger Theil–Sen + median-trimning, som er strengere robust —
  og deres klemme er 20× for bred til os (±2 % = ±25 s over et afsnit).
- **Kvantil-bootstrap af groft offset.** Vi får vores globale offset fra alass,
  som er baseret på lydenergi og er et bedre udgangspunkt.

### 8.2 P1 — monoton ankermatching: målt ud (0,6–1,4 %)
`select_monotonic_matches` vælger den højest scorende *monotone* kæde, så en
senere replik aldrig matcher en tidligere linje. Vores `_match_segments_to_lines`
er græsk og **urordnet**: hvert segment vælger bedste linje i hele ±30 s-vinduet.
Diagnosen er rigtig, men effekten findes ikke i praksis:

| model | klip med ≥1 rækkefølgebrud | ankerpar der bryder |
|---|---|---|
| tiny.en-greedy-cpu | 1/83 (1,2 %) | 1/170 (0,6 %) |
| small.en-q5_1 | 2/98 (2,0 %) | 2/215 (0,9 %) |
| turbo-q5_0 | 3/95 (3,2 %) | 3/210 (1,4 %) |

Aldrig mere end ét brud i samme klip (uklumpet), og MAD-medianen er 0,11–0,15 s
mod gaten på 1,0 s. **Droppet uden matrixkørsel.**

### 8.3 Count-gaten binder ved 16×15 — utilsigtet konsekvens af vores egen ændring
Målt undervejs, og vigtigere end selve gennemgangen:

| model | median ankre/klip | klip under count-gaten (≥3) | klip med konfident shift |
|---|---|---|---|
| tiny.en-greedy-cpu | 2 | **151/266 (57 %)** | 113/266 (42 %) |
| small.en-q5_1 | 2 | 139/269 (52 %) | 123/269 (46 %) |
| turbo-q5_0 | 2 | 149/266 (56 %) | 116/266 (44 %) |

Vi tredoblede antallet af klip, og over halvdelen bidrager ikke til
**timing**-evidensen, fordi `_robust_clip_shift` kræver 3 ankre inden for ét klip.
(De bidrager stadig til indholdsscoren — whisper-udgiften er ikke spildt.)
Ved 5×60 var der plads til 3+ ankre pr. klip; ved 15 s er medianen 2.

### 8.4 A1 fejler for tredje gang — nu på evidensmængde
Deres `align_events` accepterer et transform kun hvis det **forbedrer beviset**
(dækning + score) frem for at tærskle på parameteren. Det er præcis den mekanisme
A1 manglede. Muse pegede på at en naiv version er cirkulær hos os (vores
kandidatur er token-baseret i et ±30 s-vindue, så en lille hældning flytter ingen
par — residualerne bliver mekanisk bedre) og foreslog **split-half**: fit
hældningen på halvdelen af ankrene (skiftevis klip), evaluér ude af prøve mod en
konstant-model.

Begge kriterier blev målt på ankre fra sweep-transskripter, ingen whisper kørt:

| datagrundlag | hældningskriterium | split-half-kriterium |
|---|---|---|
| 15 raske + 2 drivende (sweep) | adskiller, margin 0,0035–0,0331 | adskiller på 4 af 5 modeller |
| **29 raske + 4 drivende (`out/`)** | **OVERLAP** (raske max 0,0302, drivende min 0,0295) | **OVERLAP** (raske max +0,075, drivende min +0,002) |

Muses advarsel holdt: "max-af-15 / min-af-2" er ikke et beslutningsgrundlag. Da
datasættet blev udvidet fra 17 til 33 afsnit, forsvandt adskillelsen for begge
kriterier. A1 har nu fejlet på tærskel-i-støj (5.4), på matrix-omkostning
(740e218) og på evidensmængde (her). **Byg det ikke — tredje gang.**

Sidegevinst: Slow Horses-afsnittene havde aldrig fået et facit. Målt på fuldt
transskript er alle fem målbare raske (|hældning| ≤ 0,0017 s/min).

### 8.5 P3 — dialog-/HI-filtrering af ankerlinjer: målt ud
Deres `dialogue_anchor_confidence` vægter skilte, titler og cues under 250 ms ned.
Muses to krav før kode:

| måling | resultat | dom |
|---|---|---|
| cues under 250 ms | **1 af 17 612** (0,01 %) | grenen er tom |
| ankre matchet til en HI-linje (klammer/parentes/♪/talerlabel) | **10 af 1561** (0,64 %) | for lille |

Årsagen er at `tokenize` allerede kræver 4+ bogstaver og fjerner stopord, så
"[MUSIC PLAYING]" og "(SIGHS)" sjældent giver 2+ delte indholdsord. Filtret
findes reelt allerede. **Droppet.**

### 8.6 TAGET: cache-nøglen manglede provider og model
Deres `cache.py` bygger et manifest med **hele konfigurationen** hashet ind, så
enhver ændring invaliderer automatisk. Vores `cache_key_for` håndsamlede fire
indstillinger:

    fingerprint : sample_count : clip_seconds : window_minutes : whisper_mode

Den cachelagrede payload **er** transskriptions-output (`samples`,
`whisper_verdicts`) — men hverken provider eller model stod i nøglen. Skifter man
`WHISPER_MODEL`, genbruges den gamle models domme lydløst, indtil selve
underteksten ændrer sig.

Rettet: nøglen slutter nu på `:provider:model`, hentet fra den samme
`correctness.full_transcript_cache_key()` som fuldt-transskript-cachen bruger, så
de to ikke kan drive fra hinanden. Fire tests i
`LineOrderCacheKeyTests`; tre af dem fejler uden rettelsen.

Matricen kunne ikke have fanget den: hver shard er ét (afsnit, model)-par med sin
egen database, så der er aldrig to modeller om den samme cache dér. Det er
præcis den fejlklasse 2.5b og cache-nøgle-uenigheden i 2.10 hørte til.

### 8.7 Ikke taget
`propose_snap_times`/`resolve_snapped_spans` (snapper cue-grænser til whisper-
ordtiming) løser deres renderingsproblem. Vi retter hele blokke, og punkt 3 har
allerede afgjort at vi ikke rører enkeltcues. Ordtider via stable-ts er desuden
faster-whisper-only, og DTW-sporet crashede os allerede (3.1).

---

## 9. Det er ikke drift. Det er en framerate-uoverensstemmelse (18/9)

Brugeren spurgte hvorfor alass ikke selv retter langsom drift. Undersøgelsen
vendte hele problemstillingen om.

### 9.1 Alle fire "drivende" afsnit er 24 mod 23,976 fps
Videoen er `24000/1001` = 23,976 fps (ffprobe). Skalér underteksten med præcis
det forhold og mål ankerhældningen igen:

| afsnit | som den er | ÷ (24/23,976) | × (24/23,976) |
|---|---|---|---|
| C_S02E04 | −0,0664 | **−0,0063** | −0,1265 |
| C_S02E06 | −0,0595 | **+0,0000** | −0,1196 |
| C_S02E11 | +0,0574 | +0,1172 | **−0,0025** |
| C_S03E04 | −0,0519 | **+0,0082** | −0,1120 |
| C_S02E01 (rask kontrol) | +0,0045 | +0,0646 | −0,0554 |

Tre skal skaleres ned, én op — begge retninger af den klassiske NTSC-pulldown.
Den raske kontrol er allerede flad og bliver **værre** under begge skaleringer.
0,06 s/min er ikke "drift"; det er 0,1 % = 1001/1000 eksakt.

### 9.2 alass KAN rette det — mekanismen findes allerede
`alass-cli` har `-g, --disable-fps-guessing`, altså er fps-gætning slået **til**
som standard, og vi slår den ikke fra. Testet ved at injicere præcis den drift på
et rask afsnit (C_S02E01, ×1,001):

| version | median afvigelse | max afvigelse | cues inden for 1,0 s |
|---|---|---|---|
| input (drivende) | +0,660 s | 1,260 s | 360/457 (79 %) |
| **alass, split-penalty 7** | **+0,197 s** | **0,198 s** | **457/457 (100 %)** |
| alass, split-penalty 3 | −16,8 s | 34,4 s | 0/457 |
| alass, split-penalty 1 | −21,5 s | 41,1 s | 0/457 |

alass fandt selv forholdet: *"'reference file FPS/input file FPS' ratio is
23.976/24"*. Ved vores egen split-penalty på 7 er driften **helt væk**. (De lave
split-penalties er katastrofale — bekræfter at 7 er det rigtige valg.)

### 9.3 Men den fyrer ikke på de ægte filer
Alle fire ægte afsnit, med både WAV og MKV som reference:

    info: 'reference file FPS/input file FPS' ratio is 1
    shifted block of 376 subtitles ... by 0:00:00.000

Prøvet uden held: `-O 0` (hastighedsoptimering fra), `-i 10`, `--no-split`, MKV i
stedet for WAV, og drift forankret i både t=0 og midten (begge syntetiske
varianter blev fundet). **Årsagen er ikke fundet.** Hypotesen — utestet — er at
alass' gæt vælger forholdet ud fra hvor meget alignment-scoren forbedres, og at
en ægte undertekst aldrig matcher perfekt selv korrekt skaleret (egen
forfatter-jitter på 0,2–0,5 s), så forskellen mellem forhold 1 og det rigtige
bliver for lille.

### 9.4 En DISKRET ratio-test adskiller, hvor enhver kontinuert tærskel fejlede
A1 fejlede tre gange, fordi den satte en tærskel på en kontinuert hældning i et
støjbånd. Men framerate-forhold er ikke kontinuerte — der er tre kandidater.
Test hver af dem og se hvilken der gør ankrene **fladest** (median absolut
afvigelse fra deres egen median). Målt på alle 39 afsnit i `out/`:

| gruppe | n | valgte et forhold ≠ 1:1 | gevinst |
|---|---|---|---|
| **drivende** | 4 | **4 af 4** | +0,100 … +0,240 s |
| raske | 35 | **1 af 35** | 34 af 35 giver præcis +0,000 |

Den ene undtagelse (C_S02E19, gevinst +0,070) har en ankerspredning på **4,1 s** —
den er en af de kendte forkert-indhold-filer, ikke en rask fil.

**Margin: 0,030 s.** Og de raske filer lander på præcis 0,000, fordi 1:1 vinder
rent — der er intet støjbånd at sidde i. Det er kvalitativt en anden slags test
end A1's, og den første der adskiller på hele datasættet.

**Endnu ikke bygget.** Skal diskuteres med Muse og køres gennem matricen først.

### 9.5 fps-korrektionen bygget og rullet tilbage — tredje gang samme metodefejl (18/9)
Koden blev skrevet (detektor, applier, pipeline-sti, indstillinger, 7 tests) og
derefter fjernet igen. Muse gennemgik den og fandt tre skibsblokerende fejl; jeg
efterprøvede alle tre selv.

**1. Stien var død.** `anchor_points` blev lagt på samples i `correctness.py`, men
hovedstiens samples bygges i `line_order.py` (6 steder, 0 forekomster af
`anchor_points`). `_fps_points(result)` var derfor altid tom, og
`_try_fps_rescale` returnerede altid None. Bevist på 360 matrixrækker: nul
`fps_ratio`, nul fps-noter. Mine 7 tests dækkede kun `plan`/`apply` på
syntetiske punkter — ingen integrationstest, som ville have fanget det.

**2. Offsettet var systematisk forkert.** `c = median(residual efter) −
median(residual før)` bevarer medianresidualen, men under en rampe er den
medianresidual = forspring + rampe(median). Korrektionen bagte derfor et
konstant skift på 0,001·t_median ind — **+0,63 s på et 21-minutters afsnit**,
±1,6 s på 50 minutter. Eftervist analytisk: ny_tid = s/k + s_med(1−1/k), altså
en konstant fejl. Det rigtige er **c = 0**: et framerate-forhold pivoterer i
filens start, så ren skalering genskaber sandheden eksakt, forspring og det hele.
Både idempotens-testen og forspringstesten bestod MED den forkerte c — de
verificerede c's definition, ikke dens rigtighed.

**3. Og det afgørende: detektoren holder ikke ved produktionens sampling.**
Produktionen placerer klip **dialog-tæt** med ±30 s vinduer
(`pick_dialogue_dense_time`, `window_minutes` = 0,5). Mine målinger brugte
jævnt fordelte klip og ±60 s. Reproduceret uafhængigt:

| sampling | ægte fanget | falske positiver | største gevinst på rask |
|---|---|---|---|
| 16 klip, **produktion** | 2/4 | **2** | +0,118 |
| 16 klip, min metode | 3/4 | 1 | +0,123 |
| 28 klip, **produktion** | 3/4 | 0 | +0,084 |
| 28 klip, min metode | 4/4 | 0 | −0,014 |

Ved produktionens sampling fyrer 16 klip **falsk på to raske filer**, og 28 klip
efterlader en margin på 0,006 mellem største rask (+0,084) og gulvet (0,09). Det
er A1's hul på 0,009 igen. Den DTW-bekræftede C_S03E04 scorer +0,005 — under
rask-max. **Der findes ingen tærskel.**

Dommen flipper desuden med transskript-model på identisk input: på
sweep-tiny fyrer C_S03E04 (+0,347) mens C_S02E04 tier (−0,105) — den omvendte
delmængde af hvad large-transskripterne gav.

**Det er tredje gang samme metodefejl** (7.1: "konklusioner målt under forkerte
forudsætninger"). Først full mod sampled, så tredjedele mod 5×60 s, nu jævnt
fordelte mod dialog-tætte klip. Læren står allerede skrevet: skriv produktionens
faktiske parametre ned FØR målingen designes. Jeg gjorde det ikke.

**Hvad der står tilbage.** Fundet i 9.1–9.4 er ægte og uændret: de fire afsnit
ER 1001/1000, bekræftet uafhængigt med DTW-ordtider, og skaleringen retter dem
eksakt. Men 0,1 % ligger under støjgulvet for ankre samplet som produktionen
sampler dem. Eneste ærlige tilbageværende mulighed er **kun i full mode**, hvor
ankrene er tætte og allerede betalt — det kræver sin egen validering ved
full-mode-sampling, og full mode er i forvejen i regression (5.14).

Beholdt: `fps_late`/`fps_early` i matricen som **tilvalg** (ikke i standardsættet
— de tester alass' egen fps-gætning, ikke vores kode, og koster 28 % køretid).
Fjernet: detektor, applier, pipeline-sti, indstillinger, tests.

### 9.6 alass' egne indstillinger gennemfejet — den kan presses til at se det, men fyrer katastrofalt forkert (18/9)
Brugeren bad om at faa alass' opsaetninger testet ordentligt, med den rigtige
begrundelse at alass burde vaere god til netop framerate-fejl. Fejet:

| greb | proevet | virkning paa fps-gaettet |
|---|---|---|
| `--split-penalty` | 0,5 / 1 / 3 / 7 / 20 / 50 / 100 / 1000 | ingen — gaettet traeffes foer alignment, ratio=1 hele vejen |
| `-g` | til/fra | slaar gaetningen fra; koster PAL-rettelsen (9.x) |
| `-O` (speed-opt.) | 0 / 1 / 4 | ingen |
| `--sub-fps-inc/-ref` | 24 / 23,976 | ingen — gaelder kun MicroDVD `.sub`, ikke SRT |
| `-n` | til | ingen |
| reference | WAV mod MKV | ingen |
| **`-i` (interval)** | 1 / 5 / 10 / 50 / 100 / **200** / 300 / **500** / 1000 | **fyrer ved 200, 500 og 1000** |

Standard er `-i 1` (ms). Ved grovere interval begynder fps-gaettet at fyre —
men ustabilt (fyrer ved 200, IKKE ved 300, igen ved 500) og med falske alarmer:

| opsaetning | drivende fanget | falske paa raske |
|---|---|---|
| `-i 200` | 2/4 (rigtig retning begge) | **3/31** |
| `-i 500` | 2/4 (rigtig retning begge) | **2/31** |

**Og de falske er katastrofale.** alass paastaar PAL-forhold paa raske filer:
C_S02E21 -> 25/23,976, C_S03E07 -> 25/24. En anvendt 25/24 paa en rask fil
flytter slutningen af afsnittet ~50 sekunder. Til sammenligning er selve
sygdommen 1,3 sekunder.

**Konklusion:** alass kan presses til at se fejlen, men kun halvdelen af gangene,
kun i et ustabilt interval-regime, og til prisen af 2-3 raske filer oedelagt pr.
31. Det er ikke brugbart. Til sammenligning: ankerruten ved 24 spredte klip giver
4/4 med 0 falske blandt 31 raske og margin 0,018 s/min.

**Fare noteret:** hvis nogen nogensinde saetter `-i` hoejt i vores pipeline for at
goere alass hurtigere, begynder den at omskalere raske filer med PAL-forhold.
`-i` skal blive paa standardvaerdien.

### 9.7 Maalt rate i stedet for diskret forhold: proevet og forkastet (18/9)
Brugeren spurgte om samme metode kunne lukke matricens `drift`-scenarie (2 %,
20x aegte). Tre aendringer proevet oven paa den committede framerate-rettelse:

1. **Spredningsgaten flyttet til residualer om det fittede linjefit.** En
   drivende fils raa spredning er +-12 s *paa grund af* driften, saa den gamle
   gate afviste praecis de filer der var vaerd at rette. Denne aendring er rigtig.
2. **Trimningen lagt om fra median til linjefit.** Ved 2 % spaender ankrene
   -27..+22 s, og +-8 s om medianen efterlod **19 af 40** ankre — ét under
   minimum, saa filen faldt lydloest gennem alle gates. Efter omlaegningen:
   tip -24,66 s over 1207 s = **maalt rate 0,97958** mod sandheden 0,98039.
   Afvigelse 0,0008, altsaa ~1 s tilbage af de 25.
3. **SUSPECT-filer maatte rettes ved stort tip**, fordi en fil mistimet med
   sekunder dumper indholdsscoren *fordi* den er mistimet.

Estimatet virker altsaa. Men matricen siger nej:

| | foer | efter |
|---|---|---|
| drift | 0,061 | 0,061 |
| drift_swap | 0,061 | 0,103 |
| **piecewise** | **0,760** | **0,687** |

**Én raekke rettet, og blokfejl faldt 0,073.** En piecewise-fil med seks blokke
der trapper monotont ligner et tip, og en global rate-korrektion paa en
blok-struktureret fil er forkert. Prisen er stoerre end gevinsten.

**Rullet tilbage til 98bff33.** Den diskrete framerate-rettelse staar; den maalte
rate goer ikke. Skal den genoptages, er det oplagte spor at kraeve at fejlen
faktisk er en RAMPE og ikke en trappe, foer en global rate anvendes — men 5.x's
forsoeg paa netop den skelnen (rampe mod spring) fejlede, fordi springmodellen
har frie parametre og vinder naesten altid.

---

# 10. Maalt rate, anden runde: monotoni, fladning og "hvor meget forklarer én linje"

21. september 2026. Brugerens to forslag til at skelne rampe fra trappe, bygget
og maalt. Kort version: **den ene idé holder ikke, den anden er ikke den der
virker — og det afgoerende fund er et helt tredje sted.**

## 10.1 Monotoni skelner ikke blok fra drift

Ideen: klip fordelt ud over filen vil ved drift vise en forskydning der vokser
monotont, mens en blokfejl springer. Maalt som Spearman-rang mellem klipposition
og forskydning.

Foerste maaling, 5 afsnit, ét seed, saa lovende ud: fps 0,1 % gav |rho| 0,63–0,89
og drift 2 % gav 0,99–1,00, mod piecewise max 0,48.

**Det holdt ikke paa flere data.** Over 420 blokfejl-puljer (35 afsnit x
{piecewise, gap} x 6 seeds) naaede fem stykker |rho| 0,91–0,95. De samme fem
klarede ogsaa fladningstesten (gevinst +2,1 til +6,7 s) og restspredningen
(0,13–0,59 s). Begge brugerens diskriminatorer fejler samtidigt paa dem.

Paa de fire **aegte** 0,1 %-filer er rho kun 0,48–0,68, mens den raske fil med
stoerst tip (C_S03E05) ligger paa 0,74. En rho-port i den diskrete framerate-sti
ville altsaa vetoe en aegte sag foer en falsk. **Monotoni er derfor maalt og
gemt i signaturen, men der er ikke sat port paa den diskrete sti.**

## 10.2 Det der faktisk skiller: hvor stor en del af puljen én linje forklarer

`keep_frac` — andelen af ankerpuljen der ligger inden for 1,5 s af ét
Theil-Sen-fit, hvor residualerne trimmes om **linjen** i stedet for om medianen.

Strukturelt, ikke tilfaeldigt: en straekning er én linje fra ende til anden;
med k blokke naar en linje omkring 1/k af ankrene.

Ablation, 914 puljer: slukkes `keep_frac` koster det 6 falske positiver,
rate-loftet 8, restspredningen 1. `tilt`, `rho` og punktgulvet koster **nul** —
de er med som redundans, ikke fordi de arbejder.

## 10.3 Medianstrimmet fit var selve blokeringen (og mit fit loeg)

`anchor_drift_signature` trimmer +-8 s om medianen. Paa en 2 %-straekning er
signalet selv +-25 s, saa trimmet smider det vaek. Derfor kunne den diskrete sti
aldrig naa den store sag — ikke fordi den ikke kunne se den, men fordi den kastede
den bort foer maalingen.

**Fejl i min egen foerste udgave:** naar linjetrimmet ikke kunne holde nok punkter,
returnerede `robust_rate_fit` den *utrimmede* pulje. Det rapporterede en restspredning
paa 3,76 s som `keep_frac = 1,00` — et staerkt fit oven paa et rod. Rettet til at
returnere None. To af de ni "missede" drift-raekker var i virkeligheden denne loegn.

## 10.4 Offline-kalibrering gav 77 %, gennem roerledningen 19 %

Fund 7.1's moenster for fjerde gang. Jeg kalibrerede portene paa puljer maalt
direkte paa de korrumperede undertekster: 189 af 245 rettet, 0 af 669 falske.

Gennem `run_one` fyrede den paa en blokfejl med det samme.

**Aarsag: alass koerer foer os og skriver filen om.** Den pulje der naar
`_try_fps_rescale` er ikke den injicerede fejl. Rigtige tal, maalt gennem
roerledningen paa 18 afsnit x 9 scenarier (216 tilfaelde):

| | rettet | falske |
|---|---|---|
| straekninger (drift + drift_swap) | **7/36** | — |
| alt andet | — | **0/180** |

Af de 144 straekningskoersler: 8 naaede aldrig grenen, 32 havde ingen linje
gennem bare 8 ankre, 76 blev vetoet, 28 rettet.

## 10.5 alass gaetter en falsk PAL-konvertering og fordobler fejlen

Maalt paa C_S03E03 med 2 % straekning. alass rapporterer "fixed (Δ29,9s)", men
resten mod facit gaar fra −3,3 s i starten til +53,9 s i slutningen — haeldning
**+6,250 %**. 1,0625 / 1,02 = 1,0417 = **25/24**. alass har gaettet PAL oven i
den eksisterende fejl og gjort filen tre gange vaerre.

Det er derfor rate-loftet maa ligge over PAL: −6,25 % er den *sande* restfejl
efter alass, og en rettelse med den maalte rate er korrekt.

## 10.6 Det afgoerende: rettelsen ligger det forkerte sted i raekkefoelgen

Maalt paa 12 afsnit, 2 %-straekning, andel cues inden for 1,0 s af facit:

| | alass alene | kun forsynk | forsynk + alass |
|---|---|---|---|
| median | 0,082 | **1,000** | 1,000 (11 af 12) |
| C_S02E01 | 0,085 | 1,000 | **0,000** |

Foer alass laeser proben raten rent: −1,988 %, −1,974 %, −2,039 % mod injiceret
2 %, med `keep_frac` 0,93 og rho −0,99 paa alle tre stikproever. Efter alass er
den samme fil 0,88 og −6,25 %.

**Forsynk alene rammer 12 af 12.** alass bagefter tilfoejer intet (median 0,30 s
mod 0,14 s) og oedelagde ét afsnit helt.

Muse maalte det samme uafhaengigt paa 18 afsnit (`whisper_gpu_staging/forsynk/`)
og fik drift A 0,06 → C 1,00. Muses "ingen regressioner" holder dog ikke:
jeg fandt én af tolv.

Konsekvensen er arkitektonisk, ikke en taerskeljustering: den maalte
rate-rettelse hoerer hjemme **foer** alass, ikke efter.

## 10.7 Matricen, tiny.en, 280 raekker foer og efter

Baseline koert fra et rent HEAD-udtraek (`VERIFYARR_UNDER_TEST`), og kontrolleret
reproducerbar: to koersler af **uaendret** kode gav 0 af 84 afvigende raekker.

| scenarie | mode | recovery foer | efter | SUSPECT foer | efter |
|---|---|---|---|---|---|
| drift | sampled | 0,061 | **0,164** | 20 | 16 |
| drift | full | 0,093 | **0,201** | 16 | 14 |
| drift_swap | full | 0,093 | **0,201** | 16 | 14 |
| drift_swap | sampled | 0,061 | 0,061 | 20 | 18 |
| piecewise | begge | 0,760 / 0,822 | uaendret | 6 / 2 | uaendret |
| clean, uniform, gap | begge | 1,000 | uaendret | 0 | 0 |

**14 raekker fik en rate-rettelse**, alle i drift/drift_swap:

| afsnit | rettelse | recovery |
|---|---|---|
| C_S03E08 drift sampled (2) | −2,01 % | 0,057 → **0,982** |
| SH_S01E05 drift/drift_swap full (4) | −6,03 % | 0,007 → **0,973** |
| C_S03E04 drift/drift_swap (8) | −1,98…−2,01 % | ikke scoret (drift_case) |

De otte C_S03E04-raekker gik fra SUSPECT til ok, fordi filen nu faktisk **rettes**
i stedet for kun at blive advaret om. 185 tests groenne (174 foer; de 11 nye).

**En bogfoeringsfaelde:** matricen afkorter `note`-feltet i jsonl, saa
`grep "rate stretch" note` fandt kun 2 af de 14. `sync`-feltet baerer sandheden.
Jeg naaede at konkludere forkert paa den afkortede tekst foerst.

## 10.8 To framerate-rettelser forsvandt — og det er matricens delte cache

`C_S03E04 clean/uniform sampled off` mistede deres diskrete framerate-rettelse
(15 → 13 paa tvaers af matricen).

Foerste mistanke var min egen kode. Den holdt ikke:

- Koert alene reproducerbart: rettelsen fyrer 4 af 4 gange.
- Koert som isoleret matrix-raekke (`--scenarios clean`): fyrer.
- Koert som en 4-raekkers sekvens med **delt db** mod baade min kode og rent
  HEAD: **begge** taber den, med identisk signatur (n=35, tilt −1,41,
  binned −1,46). Den eneste forskel er at min udgave ogsaa rapporterer rho.

Aarsagen er at et shard koerer 28 raekker i én proces med én db og én lydcache.
Naar min gren retter drift-raekkerne, udtraekkes andre klip, saa VAD-tidslinjen
og klipcachen ser anderledes ud for de senere raekker. I sekvensen ovenfor
voksede VAD-tidslinjen 99 → 132 → 156 intervaller hen over fire raekker.

Altsaa ingen produktionsregression — i produktion behandles én fil med sin egen
friske tilstand. Men det betyder ogsaa at **matricens raekke-for-raekke-bogfoering
ikke kan stoles paa naar en aendring flytter hvilke klip der udtraekkes.**
Aggregaterne (recovery mod facit) er derimod maalt pr. cue og er uberoerte.

---

# 11. Raekkefoelgen vendt om: whisper foerst, alass bagefter

21. september 2026. Brugerens beslutning efter maalingerne i afsnit 10.

## 11.1 Whispers vindue er fast 30 sekunder -- vi kastede halvdelen vaek

Et klip under 30 s bliver fyldt op med stilhed og koster det samme. Maalt paa
tiny.en, tre koersler hver:

| klip | 10 s | 15 s | 20 s | 25 s | 30 s | 35 s |
|---|---|---|---|---|---|---|
| tid | 0,66 | 0,67 | 0,65 | 0,74 | 0,74 | **1,00** |

Springet ved 35 s er andet vindue. Vores `clip_seconds = 15` betalte altsaa for
30 og brugte 15.

Kvaliteten, 24 aegte whisper-koersler paa 5 afsnit, samme startpunkter:

| | 15 s | 30 s |
|---|---|---|
| klip med konfident anker | 18/24 | **24/24** |
| ankre pr. klip | 4,0 | **7,5** |
| median-noejagtighed | 0,085 s | 0,090 s |
| positioner hvor kun den ene fandt noget | **0** | **6** |

30 s taber aldrig til 15 s. **Det korrigerer fund 2** ("daekning slaar
kliplaengde"): den afvejning var prissat i lydsekunder, men prisen er pr.
vindue. 16x15 og 16x30 koster det samme, saa afvejningen fandtes ikke.

Sidegevinst: `window_after = clip_seconds + window_minutes*60` voksede fra
45 til 60 s, og det er praecis derfor uniform +45 s nu kan ses.

## 11.2 Hele prisregnskabet, tiny.en, ét afsnit paa 21 min

| trin | tid |
|---|---|
| fuldt lydudtraek (alass kraever det) | 6,5 s |
| alass | 0,6 s |
| 16 klip, udtraek | 2,4 s |
| 16 klip, whisper | 8,5 s |
| **i alt** | **~18 s** |
| til sammenligning: hele afsnittet gennem tiny.en | 18 s |

Paa denne maskine sparer al klip-maskineriet altsaa ~9 s. Men maalet er en
**N100**: med 4 traade tager hele afsnittet 25,6 s her, og paa en N100 snarere
75-90 s, mens 16 klip lander paa ~20 s. Dér betaler samplingen sig.
Modelafhaengigt: 16 klip koster 9 s paa tiny.en, 48 s paa small.en-q5_1 og
270 s paa large-v3-turbo.

## 11.3 Den nye raekkefoelge

`screen_pair` koeber klippene og doemmer, foer alass startes.

- **rask** -> afsluttet ved screeningen. alass koerer aldrig, og dermed heller
  ikke det fulde lydudtraek -- det dyreste enkelttrin paa en langsom maskine.
- **straekning** -> raten maales paa ren evidens og rettes foer alass
- **uniform** -> forskydningen maales og anvendes foer alass
- **blokfejl** -> ingen forsynk; alass er god til dem, det er vi ikke

Klippene ligger i den videobaserede cache (nøglet paa video og region, aldrig
paa underteksten), saa rettepasset laeser dem gratis. Regionerne beregnes ud
fra videoens varighed, saa en rettet undertekst rammer de samme regioner.
Forbehold: heuristiske klip (linjeordens kandidatklynger) haenger paa
undertekstens egen timing og koebes forfra efter en rettelse.

## 11.4 En naer-fejl porten fangede

Foerste udgave gav piecewise seed 0 en global forskydning paa −5,41 s med en
spredning paa 0,23 s. Otte klip var enige -- men otte andre matchede
**ingenting**, fordi de laa i blokke uden for vinduet. Enighed alene er ikke
nok; svaret skal daekke filen.

Maalt over 10 afsnit x 6 scenarier, andel *enige* blandt de klip der matchede:

| | min | median | max |
|---|---|---|---|
| clean | 0,56 | 0,90 | 1,00 |
| gap | 0,60 | 0,95 | 1,00 |
| uniform | 0,67 | 0,75 | 1,00 |
| **piecewise** | 0,00 | 0,24 | **0,73** |
| **drift** | 0,00 | 0,13 | **0,33** |

0,80 rydder piecewise med 0,07 og beholder de tre uniform-filer hvis −45 s er
aegte. Mit foerste gaet (0,70 maalt mod *alle forsoegte* klip) vetoede baade
raske filer og uniform -- det var forkert defineret, ikke bare for stramt.

## 12.1 Keep-porten sad midt i anker-jitteren — full mode betalte prisen

Full mode var daarligere end sampled paa straekning (0,591 mod 0,800), hvilket
er bagvendt: full ejer hele transskriptet. Aarsagen var ikke evidensmaengden,
men en port kalibreret paa den forkerte stoejmodel.

Whisper-segmentstarter ligger rutinemaessigt 0,5-1,5 s fra deres cue. Paa en
aegte straekning betyder det at ~10 % af ankrene falder 1,5-3 s fra linjen.
Da `keep_frac` blev taelt med samme trim som fittet (1,5 s), laa 0,90-baren
**inde i den hale**: de taette full-puljer maalte halen praecist og dumpede
(keep 0,85-0,89), mens de tynde sampled-puljer fluktuerede omkring baren
(0,88-0,96) og bestod eller dumpede ved lodtraekning. Sampled var altsaa
ikke bedre — den var heldigere.

Loesningen er to trims: fit stramt (1,5 s), taels tolerant (2,5 s). Et loest
fit alene blev proevet og forkastet: det lader konkurrerende strukturer
traekke Theil-Sen-medianen af linjen, og resid steg over sin egen bar.

**Ensidig pr. konstruktion.** keep_s >= resid_s betyder at de beholdte punkter
altid er en delmaengde af de "paa linjen"-talte, saa keep_frac kan kun stige.
Ingen fil der bestaar i dag kan begynde at dumpe. Det er grunden til at
aendringen var billig at tage.

Maalt: straekning sampled 14/4/0 -> 18/0/0, full 10/8/0 -> 18/0/0. Alle 15
modeller: drift sampled 0,735 -> 0,964, full 0,618 -> 0,969. Nul rate-presyncs
paa raske filer, foer og efter. Commit b36a146.

**Laeren:** en taerskel kalibreret paa tynd evidens kan vaere forkert paa taet
evidens uden at nogen opdager det, fordi den tynde evidens stoejer henover
fejlen. Det er anden gang samme moenster (jf. 5.14) — og denne gang blev det
maalt i stedet for gaettet.

## 12.2 Verifikation uden daekning: syv filer der tav

Syv matrix-raekker slap stille igennem — hverken rettet eller advaret om. En
stille fil er den brugeren selv opdager, og derfor det dyreste udfald vi har.
To mekanismer, samme rod: **vi verificerede praecis de steder der ikke fejlede.**

**M2 (6 raekker).** Anker-resyncens snit lander 12-40 s forkert paa tynd
evidens. `_resync_verified` ekskluderer +-clip_seconds omkring hvert snit —
altsaa netop de baand snittet har forgiftet — og bestaar derfor med pæne
residualer mens 31 cues ligger 4,8 s galt (C_S03E03: snit 182,7 mod sandt
213,3). Rettelsen er at doemme den korrigerede fil om paa den friske
recheck-evidens, med samme signifikansregel og uden snit-eksklusioner.

**M1 (1 raekke).** Resolve beholdt et 4-bloks-fit hvor et helt blok laa 25 s
galt. Eneste vidne var eet anker paa 25,27 s, som `min_samples=2` undertrykte.
Pointen: **en forkert region giver INGEN ankre, aldrig et lille et.** Et enligt
indholds-matchet anker paa 10 s+ er derfor en anden fysik end den jitter
min_samples blev maalt paa, og rutes nu til anker-grenen — aldrig et verdict
i sig selv.

Alle aendringer er verdict-only: ingen cue flyttes anderledes. Maalt over alle
15 modeller, 2x4200 raekker: **71 parvise aendringer, alle stille -> advaret,
0 hvor recovery aendrede sig, 0 nye falske alarmer, 0 rettelser tabt.** Stille
i alt 104 -> 33, og 0 tilbage paa tiny.en. Commit 4b90f0b.

Ikke FP-maalbart i matricen: huge-triggeren og det udvidede safety-net har
ingen raske multiblok-raekker at fyre paa. Boer oejes paa foerste
vildtkorpus-koersel (60 afsnit i `whisper_gpu_staging/out/`).

## 12.3 Matrixtallet 18/18 var cache-sminket

Kontrolforsoeg med eksakt DB-priming: paa en frisk DB er straekning sampled
**17 rettet + 1 advaret**, ikke 18/18. SH_S01E06 har 18 af 20 ankre og holdes
tilbage af punktgulvet; i matricen er shard-DB'en primet til 46 ankre, og saa
fyrer presynken. Begge udfald er sikre, men tallet er tilstandsspecifikt.

Sidefund samme sted: **full og sampled deler IKKE shard-DB** — sampled primes
kun af sampled-raekker. En tidlig diagnose primede forkert med clean/full og
maatte koeres om.

Dette er femte gang finding 7.1's moenster rammer: et tal maalt under
akkumuleret tilstand er ikke det tal produktionen ser.

## 12.4 Nul matrix-fyringer betyder ikke doed kode

`_try_stretch_rescale` fyrer 0 gange i 4200 matrix-raekker, og efter at
presync-testen blev udvidet til at acceptere begge veje er der ingen test der
haevder den fyrer. Den ligner doed kode.

Den er det ikke. Den er den eneste straekrettelse paa hver sti hvor
screeningen ikke koerer: `bazarr.py` uden en `conn` (saa staar den gamle
raekkefoelge), `jobs.py` naar en screening kaster, og enhver fil screeningen
kalder "unknown". Noteret i docstringen (commit 1aee552) saa nullet ikke bliver
laest som oprydningsmateriale naeste gang.

**Laeren:** naar en aendring flytter arbejdet fra én gren til en anden, er
"den gamle gren fyrer aldrig mere" et spoergsmaal om hvilke stier testbaenken
daekker — ikke et bevis for at den kan slettes.

## 13.1 Scenarierne testede ikke det der staar paa skiltet

En gennemgang af hvad matricen faktisk injicerer fandt tre slags huller.

**Skiltene passede ikke.** `gap` hed "cut version", men den sletter cues og
lader alle overlevende tidspunkter staa -- det er en undertekst med et hul.
En aegte cut flytter ogsaa alt efter snittet. Omdoebt til `missing_middle`,
og den aegte fejl tilfoejet som `cut_version`. `drift`s etiket
"25<->23.976fps style" var forkert: det forhold er +4,27 %, ikke 2 %.

**Reelle fejltyper manglede helt.** PAL (25/24, +-4,167 %) er den
almindeligste aegte rate-fejl og den alass selv gaetter paa uopfordret --
totalt utestet. Rate+offset kombineret var utestet, og det er det eneste
der bruger den fittede intercept, fordi `corrupt_drift` pivoterer i t=0.
Negativ uniform var utestet, selv om ankervinduet naar clip_seconds+30 s
frem og kun 30 s tilbage -- de to fortegn er ikke samme proeve.

**Konklusioner baaret af ét terningkast.** Tre uafhaengige traek af samme
blokfejl (rettet/advaret/stille, full):

| traek | udfald |
|---|---|
| `piecewise` (den vi altid har koert) | 12 / 6 / **0** |
| `piecewise_b` | 12 / 4 / **2** |
| `piecewise_c` | 6 / 6 / **6** |

Tredje traek giver seks stille raekker hvor det foerste gav nul. "0 stille
tilbage paa tiny.en" var ét heldigt bloklayout, ikke en egenskab.

Resultat: 23 scenarier, alle som standard, 920 raekker mod 280 foer.
Nul falske positiver paa raske filer holder. `wrong_episode` er
moenstereksemplet: alass foreslaar skift paa 41-852 s, alle afvises, filen
staar uroert, og den flages -- 36 af 36. Commit b788fef.

Sidefund (fikset): `corrupt_uniform` skubbede tidlige cues under nul ved
negative forskydninger. De droppes nu og rapporteres via `kept`; klipning
til nul ville forvandle en konstant forskydning til en stykvis fejl i
filens hoved -- altsaa maale noget andet end scenariet hedder.

**Det stoerste nye hul:** `cut_version` sampled slipper stille igennem.
Screeningen siger "already in sync" paa en fil hvor 300 s af cues er
forkerte. `agree_frac` maales blandt de klip der MATCHEDE; klippene efter
snittet matcher ingenting og taeller derfor ikke med, mens klippene foer
snittet er enige om nul. Enighed er ikke daekning. `match_frac` beregnes
allerede i `screen_pair` og porter ikke paa noget.

## 13.2 Facit er sundt -- maalt, ikke antaget

Matricen scorer mod de ukorrumperede fixtures. Det maaler SELV-genskabelse:
om vi kan bringe en fil tilbage hvor den kom fra, ikke om DER er rigtigt.
En systematisk absolut fejl i facit ville vaere usynlig -- og ville straffe
en roerledning der synkroniserer korrekt til lyden.

Maalt mod DTW-ordtiderne i `tests/data/reference/*.words.json`, to
uafhaengige metoder:

| metode | median afvigelse |
|---|---|
| verifyarrs egen `clip_anchors` mod pseudo-segmenter af DTW-ord | +0,135 s |
| direkte: hvornaar lyder cue'ets FOERSTE ord vs. cue-starten | +0,205 s |

0 af 10 afsnit over 0,5 s. Kvartilerne er stramme (p25-p75 +0,09..+0,40 paa
alle seks SH-afsnit), og haeldningen hen over et afsnit er +0,26 s i median
-- ingen drift i facit selv. De +0,2 s er normal lead-in.

`afsnit_tillid.json` angiver `best_offset_s` 1,35-1,9 s for de samme ti
afsnit, og det tal er aegte nok -- men det maaler den forskydning der
maksimerer ord-enighed, ikke at underteksten ligger 1,7 s for tidligt. Min
foerste forklaring (et ord midt i et cue lyder en halv cue-laengde efter
cue-starten) blev AFVIST: halvdelen af mediancue-varigheden er 1,02 s, ikke
1,73 s, og korrelationen mellem de to er -0,89 -- modsat af forudsigelsen.
Hvad den metrik praecist fanger, ved jeg ikke.

Men konsekvensen kan afvises uden at vide det: laa facit 1,73 s for tidligt,
ville cue'ets foerste ord lyde 1,73 s efter cue-starten. Det lyder 0,2 s
efter, med p75 paa 0,3 s. En systematisk fejl paa 1,7 s er uforenelig med
den fordeling. Tredje bekraeftelse: `uniform_p15` (+1,5 s) rettes 18/18
tilbage til facit med p50 0,044 s -- var facit 1,73 s for tidlig, ville
facit+1,5 s vaere naesten korrekt, og roerledningen skulle have ladet den
staa.

## 13.3 Den delte shard-DB er ikke entydigt optimistisk

Klip-cachen er noeglet paa (video_path, region_index), saa hver raekke
arver de klip dens forgaengere koebte. Produktionen moeder hver fil én gang.
`--fresh-db` giver én engangs-DB pr. raekke, saa tallet kan maales.

| arm | rettet | advaret | stille |
|---|---|---|---|
| delt DB | 636 | 76 | **44** |
| frisk DB | 620 | 110 | **26** |

53 parvise skift af 920. Jeg forventede at frisk DB ville vaere entydigt
vaerre. Det er den ikke:

- 17 raekker rettet -> advaret (prisen: uden varme klip toer den ikke rette)
- 22 raekker stille -> advaret (gevinsten: tyndere evidens giver ogsaa
  daarligere resync-planer, som B1b-dommen saa fanger)

Den delte DB er altsaa optimistisk paa RETTELSER og pessimistisk paa
ADVARSLER. Det aerlige tal for stille gennemloeb er 26, ikke 44 -- men med
34 faerre rettelser. `cut_version` sampled halverer sine stille raekker fra
12 til 6; `uniform_m5` gaar den anden vej, 18/0/0 bliver 16/0/2.

**Laeren:** begge tal er rigtige, de maaler bare forskellige ting. Citer
aldrig delt-DB-tallet som hvad en foerste koersel goer. Commit d0dfa78.

## 13.4 En ventesloejfe der matchede sig selv

`until ! pgrep -f "e2e_matrix_parallel"` afslutter aldrig: sloejfens egen
kommandolinje indeholder moensteret, saa `pgrep` matcher sit eget skal.
Seks af dem hobede sig op, tre i 5-7 timer, og statustjek med samme moenster
svarede "koerer" paa min egen grep -- saa jeg rapporterede en koersel som
igang 14 minutter efter den var faerdig.

**Laeren:** et procestjek skal udelukke sig selv (`pgrep -f "[e]2e_matrix"`
eller tjek paa pid), og en ventesloejfe skal have et loft. Det er samme
familie som fund 7.1: maaleapparatet loej, ikke koden.

# 14. Eskalering: sampled-mode var blind for halvt reparerede blokke

## 14.1 Sampled-evidensen indeholder ikke signalet -- maalt tre veje

De lydloese blokfiler i sampled har ankre der er ENIGE. Den forskudte tekst
matcher ikke lyden der hvor den staar, saa den giver aldrig et anker; de ankre
der findes, sidder paa den korrekte del. Tre regler proevet, alle doede:

| regel | falske pos. | fanget |
|---|---|---|
| trin > 10 s | 0/434 | 2 af 8 |
| for faa ankre (N=10) | 256/514 | 8 af 8 |
| laengste ankerfri stime | raske naar 11 | lydloese 3-10 |

"Confirmed swapped by Whisper" er heller intet signal: raske Community-filer
har 46 af dem. **Laeren:** fravaer af ankre beviser intet, uanset om man
taeller dem eller maaler hvor de mangler. Eneste vej er at koebe mere evidens.

## 14.2 Eskaleringen var slaaet fra -- og kraevede det forkerte

`escalate_sampled_to_full` stod paa False med en kommentar ("buys almost
nothing") maalt foer `escalate_only_multi_block` fandtes. Og selv slaaet til
kraevede den at ankrene var UENIGE (spredning > 5 s eller 2 residualer) --
netop det de halvt reparerede filer ikke er. alass' egen flerbloksfit er
mistanken; den er nu nok i sig selv.

## 14.3 Vi koebte transskriptet og smed det vaek

Efter eskalering faldt resync og recheck tilbage til de 16 samplede klip,
fordi de valgte evaluator paa `cfg.whisper_mode`. Dommen blev taget paa den
evidens vi lige havde betalt os fra. Rettet med en `ev_cfg` (full) til resync
og recheck. Cache-noeglerne bliver paa cfg.

## 14.4 ... men ikke i tvetydighedsloesningen

Foerste udgave gav `ev_cfg` ogsaa til `_resolve_ambiguous_sync`. Resultat: 36
naesten perfekte Slow Horses-blokreparationer (rec 0,93-0,997) endte paa
0,27-0,53. Noten sagde "blocks: no evidence; old: no evidence" -- full-
evaluatoren finder ingen evidens for de to kandidater, saa 'new' vinder som
eneste med evidens. Flagget, saa ingen tavs fejl, men en god fil blev
oedelagt. **Laeren:** klassetal skjuler det; kig altid paa rec paa de
raekker der BLEV i klassen "advaret".

## 14.5 En maaling jeg selv ugyldiggjorde

To gange koerte jeg matricen forkert: foerst uden `--fresh-db` (arvet
DB-tilstand flyttede raekker der slet ikke eskalerede), saa uden `--only` for
de 8 ekstra afsnit (standardlisten har kun 10). Referencen skal koeres med
praecis samme flag, ellers er diffen stoej.

## 14.6 Et anker paa 10 s+ er nok til at kigge

0 af 283 sampled-filer der ender korrekte og uflaggede har et anker >= 10 s.
Som eskaleringstrigger: arm 2 lydloese 10 -> 8, nul andre celler aendret, og
paa rigtige data bliver 3 aegte ude-af-sync-filer flagget, bl.a. C_S03E01 der
foer blev erklaeret "already in sync".

## 14.7 Framerate-triggeren brugte rettelsens porte

To RIGTIGE 24->23,976-afsnit (C_S02E04, C_S02E06) rettes i full men slipper
tavst igennem i sampled. Sampled ser driften (62 ankre, tilt -1,39 s mod full
-1,40 s), men triggeren til at koebe transskriptet brugte de samme strenge
porte som selve rettelsen: C_S02E04 faldt paa binned -0,879 mod 0,90.
Matricen fangede det aldrig -- dens fps-scenarier er syntetiske og rene.
**Laeren:** en trigger skal have en lavere bar end det den udloeser.

## 14.8 Facit er for groft paa C_S03E16

Klassificeret "rigtigt-indhold-ude-af-sync", men tredjedelene er -0,30,
-0,82, -0,40 s: ikke monotont, altsaa ikke framerate, men en lokal bule under
1 s. Koden lader den vaere i begge modes, og det er formentlig rigtigt.

## 14.9 C_S02E04 flagges af tiny.en i full

`clean full` og `dropdup full` er SUSPECT allerede foer eskaleringen: to
ankre ved 445/460 s staar begge -4 s. Med large-v3-turbo paa de rigtige data
flagges afsnittet ikke. Eskaleringen giver `dropdup sampled` samme dom. Ikke
en ny fejlklasse, men en kendt full-mode-falsk-positiv der nu ogsaa rammer
sampled paa det afsnit.

## 14.10 SH_S01E04: "falsk positiv" er maaske en aegte lokal fejl (Muse)

Min hypotese -- en region der vender tilbage til naboens niveau er
fejlmatchede ankre -- er modbevist. Muse (SPAR_OE_SVAR.md) fandt 3 matrix-
planer hvor en saadan oe under 5 s er en AEGTE injiceret blok (C_S02E01,
C_S02E02, C_S02E09 piecewise full, alle inden for 0,4 s af facit). Reglen
ville droppe dem.

Og de 3 ankre i SH_S01E04 er ikke gentaget tekst: ca. 10 forskellige
talesegmenter over 50 s (1367-1427 s, ca. 22:47-23:47) viser alle cuen 2,5-4 s
for sent, mens naboerne ligger paa 0. Det ligner en aegte lokal forsinkelse i
underteksten -- i saa fald er omskrivningen korrekt, ikke en falsk positiv.
Kan ikke afgoeres indefra; kraever at nogen ser scenen.

Muses forslag (anchor_suspect_min_samples 2 -> 3 i full) er afvist af mig:
dens egne tal giver 12 REMAINS-raekker der gaar til ok med rec ned til 0,78,
altsaa nye tavse raekker. Samme moenster ses paa C_S02E04 (to ankre ved
445/460 s, -4 s): kort lokal forskydning, aegte eller ej er uafklaret.
**Laeren:** "Slow Horses er korrekt" er et facit paa afsnitsniveau; det siger
ikke noget om en enkelt scene paa 50 s.

## 14.11 Blokresten: under alle taerskler, men den holder sit fortegn

De sidste 6 tavse full-raekker (C_S02E01/C_S02E02/SH_S01E02 piecewise) havde
efter resync alle ankre inden for +-2,5 s -- men i lange stykker paa samme
side: 12 ankre paa +1,6 s, 13 paa -1,6 s. Muse fandt mekanismen: resync lagde
EEN kompromisregion (+9,18 s) over tre blokke der skulle have +10,39/+9,04/
+7,43 -- een ramt, een under-, een overkorrigeret. Regel: 10 sammenhaengende
ankre med median >= 1,2 s fra filens. Korrekte filer topper ved 0,86-0,87 s
(matrix OG alle 52 rigtige, koert paa alle filer), resterne ved 1,54-2,14 s.
Arm 2 tavse 8 -> 2. Commit 7097bb0.

Sidefund: `row["correctness_samples"]` blev ikke opdateret efter en
fps/stretch-recheck -- tre uniform_neg-raekker saa ud til at have 22 s
vinduer, men det var rammen foer rettelsen. Samme fejlklasse som 6.1/6.2.

## 14.12 Skaermen maalte kun de klip der matchede

`match_frac` blev beregnet og aldrig brugt. C_S03E08 cut_version sampled: 5 af
16 klip matchede, alle i de foerste 405 s, alle enige -- "ok", alass koerte
aldrig. match_frac alene skiller ikke (raske filer ogsaa 4-5 af 16), men en
klippet udsendelse slutter klippets laengde foer lyden: 52 rigtige afsnit
slutter hoejst 55 s foer, cut_version >= 300 s. Over 150 s -> "unknown", saa
den normale kaede koerer. Arm 2 tavse 2 -> 0, og 12 SH-cut_version-celler gaar
fra advaret til RETTET, fordi alass nu faar lov. Commit b45716f.

## 14.13 Status efter afsnit 14 (tiny.en, 1656 celler)

| | start | nu |
|---|---|---|
| arm 1 tavse | 6 | 0 |
| arm 1 rettet | 852 | 864 |
| arm 1 advaret (ikke rettet) | 26 | 20 |
| arm 2 tavse | 14 | 0 |
| arm 2 rettet | 156 | 168 |
| clean/dropdup | uaendret | uaendret |

Rigtige afsnit, sampled: ude-af-sync tavse 4 -> 1 (C_S03E16, den lokale bule,
jf. 14.8). Ingen Slow Horses-fil flagget; SH_S01E04 full omskrives stadig
(jf. 14.10, afventer brugerens facit for scenen 22:47-23:47).

## 14.14 Matricen gemte ikke det fulde transskript -- produktion goer (Muse)

`patch_whisper_full` returnerede transskriptet uden at gemme det i cachen.
Produktion gemmer (`generate._transcribe_or_reuse` ->
`save_full_transcript_cache`), og `evaluate_against_full_transcript` LAESER
cachen og returnerer None uden den. Full-raekker blev pre-seedet i
`run_one`, saa de var upaavirkede -- men en ESKALERET sampled-raekke havde tom
cache. Foelger:
- `_try_anchor_resync(ev_cfg)` kunne aldrig planlaegge i matricen efter
  eskalering (derfor skiftede eskalerede raekker fra "N anchor region(s)" til
  "N sync block(s)" efter a57d35f). I produktion kan den.
- Den forkastede esc_ev-kurs ("blocks: no evidence; old: no evidence") var
  et harness-artefakt, ikke evaluatorens adfaerd.
- genuine.py bruger samme patch: samme gab for rigtige sampled-raekker.
Dagens eskaleringstal (a57d35f-b45716f) beskriver derfor ikke produktion
praecist for eskalerede sampled-raekker. Harness rettet; HEAD maales igen.
**Laeren:** en patch der erstatter en funktion med sideeffekter skal ogsaa
efterligne sideeffekterne -- ellers maaler man patchen, ikke koden.

## 14.15 To matricer paa een gang forurener hinanden

Shards navngives efter indeks, ikke efter --out: to samtidige koersler deler
DB, arbejdsmappe og log pr. shard. Resultatet var en log skrevet af to
processer (et halvt "Δ" -> UnicodeDecodeError) og, havde de gennemfoert,
blandet cache-tilstand. Koer altid matricer efter hinanden.

## 14.16 Jeg optimerede mod et beregnet facit -- og lavede to falske rettelser

`afsnit_tillid.json` kaldte C_S02E04/C_S02E06 "ude af sync" (framerate-drift,
tredjedele -0,15/-0,56/-1,06 s). Full-mode "rettede" dem; jeg saenkede
sampled-triggeren (0750587) saa sampled goer det samme. Brugeren tjekkede:
INGEN drift. Rullet tilbage i b93f8cc. Full-modes egen framerate-rettelse paa
dem er en aaben falsk positiv -- anker-tilt (-1,40 s) og uafhaengig VAD-tilt
(-1,06 s) er enige, og WAV/video-laengde er identisk (0,005 %), saa aarsagen
er ukendt. Afventer brugerens tjek af konkrete replikker (C_S02E06 16:11/16:13).
**Laeren:** falske positiver afgoeres kun paa filer brugeren har bekraeftet.

## 14.17 To ankre af 51 er stoej

SH_S01E04 full (bekraeftet korrekt) blev omskrevet paa 2 ankre >= 2,5 s.
Triggeren brugte sampled-barren (2 af ~16) paa taet full-evidens. Nu 3 ved
taet evidens (Muse, 240c607). Samme aendring fjernede C_S02E04's flag ved 7:25
(ankerparret 445/460 s), som brugeren ogsaa har bekraeftet er korrekt.
Matrix: 0 klasseskift, 0 rec-aendringer.

## 14.18 Status efter afsnit 14 (rettet harness)

| | start i gaar | nu |
|---|---|---|
| arm 1 rettet / advaret / tavse | 852 / 26 / 6 | 884 / 0 / 0 |
| arm 2 rettet / tavse | 156 / 14 | 184 / 0 |
| bekraeftede filer med falsk positiv | SH_S01E04, C_S02E04 x2 | C_S02E04/E06 full framerate |

## 14.19 Den "falske" framerate-rettelse var rigtig -- brugeren saa den danske fil

Ved hver Community-video ligger en engelsk (.en.srt/.en.hi.srt, den verifyarr
maaler) OG en dansk (.da.srt). Brugeren tjekkede formentlig den danske. Maalt
med programmets egen, sprog-uafhaengige VAD-tilt:

| afsnit | engelsk | dansk |
|---|---|---|
| C_S02E04 | -1,06 s | -0,14 s |
| C_S02E06 | -1,07 s | 0,00 s |
| C_S02E11 | +0,90 s | +0,03 s |
| C_S03E04 | -0,71 s | +0,39 s |

Engelsk minus dansk driver 0,85-0,91 s fra start til slut. Instrumentet er
altsaa enigt med brugerens oeje paa den danske fil, og den engelske har reel
0,1 %-drift. 14.16's "falske positiv" var en misforstaaelse om HVILKEN fil;
b93f8cc's tilbagerulning af sampled-triggeren genindfoert.
**Laeren:** naar brugeren bekraefter en fil, saa spoerg hvilket sprog -- og
en uafhaengig maaling (her VAD) kan afgoere en modsigelse uden at vende en
beslutning paa et gaet.

## 14.20 Jitter fanges nu -- sidste lydloese scenarie lukket

C_S02E03 jitter (+/-1-3 s tilfaeldigt pr. cue) gik lydloest igennem i alle 4
celler (rec 0,082, flag ok). Aarsag: hvert ankers *skift* er en median over
klippets linjer, saa stoejen middles vaek, og ankrene ser raske ud. Stoejen
overlever kun *inde i* ankeret som spredning (MAD) mellem klippets egne linjer.

Regel (kun detektion, ingen rettelse -- der er ingen offset at rette):
median af ankrenes MAD >= 0,5 s over mindst 5 ankre -> SUSPECT "Cue timing is
noisy ... fetch a fresh subtitle". Ligger i den endelige ok-gren, saa den kun
kan ramme filer der ellers ville blive godkendt.

Maalt (tiny.en, 1656 raekker, `matrixdata/eskalering_2026-09-23/hx_jit*.jsonl`):
- Jitter: 64 advaret + 4 lydloese -> 68 advaret, 0 lydloese.
- Alle andre 1652 celler: 0 klasseskift, 0 flag/sync-aendringer.
- Rask top: 0,39 s (full) / 0,445 s (sampled, SH_S01E01 uniform_neg, rettet).
  Jitter-raekken der kun fanges af reglen: 0,56 s. Margin paa sampled-siden er
  tynd (0,055 s) -- tjekkes igen ved kryds-model-koersel (tiny er vaerst).
- 52 aegte afsnit: byte-identisk med foer. Ingen Slow Horses-fil roert/flagget.

Test: C_S02E03 jitter full skal advare; SH_S01E01 uniform_neg sampled (rask top)
maa ikke. Mutationer (taerskel 0,44/0,6, min-ankre 3, gren slaaet fra) fanges.
Obs: hurtige mutationstjek kan snydes af foraeldede .pyc (samme sekund + samme
filstoerrelse) -- ryd __pycache__ mellem mutationer.

## 14.21 Manglende midterparti er aldrig blevet maalt -- og detekteres ikke

Brugerens regel 2026-09-24: fejltyper testes KUN med indlagte fejl i
bekraeftet korrekte undertekster (de 6 Slow Horses). Ved at laese SH-raekkerne
i `hx_jit.jsonl` alene: `missing_middle` er 24/24 `flag=ok`, uroert. Harnessen
har `missing_middle` i `NO_CHANGE_SCENARIOS` og taeller "uroert" som succes --
men maalet siger at manglende midterparti skal DETEKTERES 100 %. Min
tidligere "alle scenarier 100 % undtagen jitter" var derfor forkert.

Maalt (tiny.en-sweep): stoerste hul med tale i raske SH-filer 53,7 s tale / 87
ord; indlagt 300 s-hul 126-237 s tale / 333-734 ord. God margin. Opgave til
Muse: `/home/hammer/overfit/OPGAVE_MISSING_MIDDLE.md`.

## 14.22 Line order flagger bekraeftet korrekte Slow Horses-filer

Aegte SH-koersler (`hx_jit_genuine.jsonl`): E01 1, E02 3-5, E05 1, E06 3
blokke "flagged for manual review"; SH_S01E02 full endda "1 block confirmed
swapped by Whisper". Alle SH er korrekte ifoelge brugeren -> falske
positiver i line order (kun note, correctness-flag er ok). Matrix-clean paa SH:
20/24 celler har line-order-flag. Ikke undersoegt endnu.

## 14.23 Jitter-reglen gav falske positiver paa andre modeller -- rettet (53ba330)

Muse (JITTER_REVIEW_SVAR.md) regnede tvaers af 15 modeller paa korrekte SH:
full-mode topper paa 0,295 s, men sampled-medianer over 5-6 ankre naar 0,65 s
(turbo SH_S01E02) og 0,565 s (small.en SH_S01E06) -> falsk "Cue timing is
noisy" paa perfekt rettede filer. "tiny er vaerst" var forkert.

Rettet: jitter doemmes kun paa fuld daekning; sampled med median >= 0,4 s
koeber det fulde transskript (samme eskalering som screen/fps). Muse
genmaalt (JITTER_RECHECK_SVAR.md): 0 jitter-flag paa 356 korrekte SH-raekker
over 15 modeller, 3 ekstra transskripter paa 178 sampled-raekker, alle
jitter-raekker stadig fanget. SH-matrix (tiny) og 52 aegte afsnit uaendret.

Laering: en taerskel maalt paa een model er ikke en taerskel. Tjek tvaers af
modeller paa de bekraeftede filer foer commit.

## 14.24 Aabent: medium.en-q5_0 flagger korrekt SH_S01E01 (full)

Tre ankre -4,0/-4,5 s ved 201/2907/2925 s -> anchor-eskalering -> SUSPECT
paa clean og uniform_neg full. Praeeksisterende, ikke jitter. Falsk positiv
paa bekraeftet fil. Samme familie som SH04 (faa ankre der peger forkert).

## 14.25 Manglende midterparti detekteres nu (d9a7ad2)

Muse byggede og maalte (MISSING_MIDDLE_RAPPORT.md): cue-hul hvis fulde
transskript har >= 90 s tale OG >= 200 ord -> SUSPECT, ingen omskrivning.
Sampled: cue-hul >= 120 s koeber det fulde transskript. Raske SH-huller paa
15 modeller topper paa 133 ord / 112 s (ikke begge paa een gang); indlagte
300 s-huller 330-734 ord. Taler-sekunder alene adskiller IKKE paa tvaers af
modeller (credits-stykke 112 s) -- ordkravet baerer. Ufiltreret transskript
gav en turbo-hallucinationsloekke paa 653 ord i et rask hul; produktionens
egne filtre (nonspeech + repetitionsloekker) bruges derfor.

SH-matrix: 24/24 detekteret (foer 0/24), 528 oevrige celler uaendret. 52
aegte afsnit: ingen flag-aendring, ingen Community flagget. Pris: SH_S01E01/E02
har naturlige huller paa 178/174 s og koeber det fulde transskript i sampled
(en gang pr. fil). Ordgraensen haevet fra Muses 150 til 200 for margin.

## 14.26 15-model SH-matrix (sh_all15, d9a7ad2) -- hvad der fejler hvor

Rene: medium.en, medium.en-greedy, small.en, small.en-q5_1, tiny.en-cpu,
tiny.en-greedy-cpu. Fejl: drift paa SH_S01E06 (9 modeller; 2 LYDLOESE forkerte
rettelser: base.en-cpu 0,49, tiny.en-q5_1 0,81 -- alass gaetter PAL, stretch
retter alass' fejl), medium.en-q5_0 SH_S01E01 (kollapsede tidsstempler, 3
spredte -4 s-ankre -> SUSPECT i alle celler), turbo SH_S01E01 (5 ankre -3,7 s
-> resync omskriver korrekt fil; missing_middle/swap misses via post-resync-
ok-grenen), lydloese blok-misses turbo/turbo-q5_0/medium.en-q5_0. Muse-opgave:
OPGAVE_DRIFT_E06.md. Brugerregel: modeller der konstant fejler droppes.

## 14.27 alass igen efter grov rettelse (realass_2026-09-24.jsonl)

| celle | pipeline | alass paa korrupt | alass efter pipeline |
|---|---|---|---|
| tiny.en-greedy E06 drift s | 0,964 | 0,063 | 0,964 |
| tiny.en-q5_1 E06 drift s | 0,81 | 0,063 | 0,964 |
| base.en-cpu E06 drift s | 0,491 | 0,063 | 0,964 |
| turbo E04 piecewise_c f | 0,755 | 0,975 | 0,921 |
| turbo-q5_0 E04 piecewise_b s | 0,803 | 0,157 | 0,935 |
| turbo E01 clean f | 0,959 | 1,0 | 0,959 |
| tiny E01 piecewise f | 0,964 | 0,855 | 0,964 |
| tiny E03 piecewise_b f | 0,966 | 0,966 | 0,966 |

alass som finpudsning efter grov rettelse loefter alle halvgode rettelser
>= 0,92 og goer aldrig noget vaerre. Men den reparerer ikke en for kort
fejlflyttet blok (turbo clean 0,959) -- ankerstoej skal stoppes FOER den
rammer filen. turbo E04: alass alene var 0,975, ankrene oedelagde det.

## 14.28 VAD-lokal justering af ankre (brugerens ide, vadalign.py)

Silero VAD (whisper-vad-speech-segments, lokal) + moenstermatch af cue-
intervaller mod taleintervaller i +-15 s: indlagte +4/-3,7/+1,5/0 s findes
inden for 0,15 s paa 20/30/40 min. Ved medium.en-q5_0's FP-ankre (48:35,
Whisper -4,5 s) siger VAD -0,55 s -> ville afvise. Svaghed: raab+musik
(3:21) giver -6,8 s -> skal have sikkerhedsmaal (skarp top). Whisper-tags
(musik/raab) er modelafhaengige: turbo-q5_0/q8_0 tagger aldrig (0 tags),
medium.en 62; kan kun bruges som ekstra veto.

## 14.29 Testsaet uden byttede linjer (brugerens oenske)

`rapport/swap_scan.py` maaler byttede to-linjers cues pr. afsnit mod turbo-
transskriptet, uafhaengigt af pipelinens dom (line order koerer ikke paa
filer der allerede er doemt blok/forkert indhold -- C_S03E05 stod som 0/0
trods massiv swap). Tydelig todeling: SH og 22 Community <= 0,067; 24
Community 0,15-0,54 (fx C_S02E04 "has a space simulator. / City College").
`rapport/testsaet.json`: baser til indlagte fejl = 6 SH; rigtige fejl
bekraeftet uden swap = C_S02E08, C_S02E11 (+ forkert indhold C_S02E16/E17).
Alle bekraeftede blok-afsnit (C_S03E01/E02/E05) har swap -> ingen blok-
daekning endnu. 13 swapfri Community-afsnit venter paa brugerens dom.

## 14.30 Uafhaengigt review: matricen har formet taersklerne

Opus-agent (rapport/DETECTION_REVIEW.md, probe i matrixdata/review_probe_2026-
09-24/, 390 raekker, tiny, realistiske varianter). 18/18 rene kontroller
uroert -- men realistiske fejl slipper igennem: manglende parti 90 s 0 %,
120 s 47 % (hul altid ved 40 % i matricen; 200-ordsbarren skyldes titelsangens
note-tegn); afkortet start/slut 0 %; blok 180 s x 3-6 s tavs 17/24 naar screen
siger ok; drift 0,3 % tavs 9/18 (STRETCH_MIN_TILT_S i sekunder); screen-
tolerance 0,25 s inden i Whispers bias (C_S02E12 0,4 s uretttet i sampled);
`_confirmed_in_every_block` altid False ved single-block -> "old" kan aldrig
vinde -> jitter-filer flyttet op til 31 s (bekraeftet ved kodelaesning).
Min "552/552 paa SH" gjaldt kun matricens faste scenarier.
Plan: 1 swap-trin, 2 old kan vinde (Muse: OPGAVE_SWAP_OG_OLD.md), 3 blok/hul i
realistiske stoerrelser, 4 drift i procent + alass/VAD-finpudsning + slutkontrol,
5 matrix med tilfaeldige stoerrelser/placeringer.

## 14.31 Swap-trin (c19d585) og "originalen kan vinde" (a89ebec)

Muse byggede, jeg verificerede (SWAP_OG_OLD_RAPPORT.md). Swap: timing-uafh.
scan foer nogen rettelse skrives; rate >= 0,10 og >= 5 af >= 10 -> "hent ny",
intet skrives. Aegte: 48/48 swap-raekker flagget, 0 aendringer i de fire rene
testsaet. Old-wins: single-blok talte aldrig som bekraeftet -> "old" kunne
aldrig vinde; nu veto naar vinderen har >= 3 ankre > 2,5 s ude og originalen
er klart bedre. SH-matrix: arm 1 312/312, 4 jitter-raekker nu uroerte i stedet
for omskrevne. Suite 310 groen (9 nye tests).
Aaben: test_sync_verification.HEALTHY_SLUGS = C_S02E15 .en.srt -- ikke
bekraeftet og maalt +0,58 s median / 62 % inden for 1 s. Boer flyttes til SH.

## 14.32 Skips og blokke i realistiske stoerrelser (jeg selv, efter Muses ufaerdige start)

Tilfaeldige scenarier (seedet, SH, tiny): huller 60-300 s ved 10-90 %,
afkortet start/slut 60-300 s, 1-3 blokke 90-600 s x +-2-20 s overalt.

Skips: tale i hullet (produktionsfiltre + note-tegn-filter). Raske huller inkl.
hoved/hale topper paa 34 ord / 20,8 s; skips >= 10 linjer giver >= 71 ord.
Barre 50 ord / 15 s -> 100 % af skips >= 10 linjer (huller, start, slut).
5-7 linjer (34-37 ord) kan ikke skilles fra baggrundssnak -- graensen.

Blokke: klip-ankre kraever 3 linjer pr. klip, og produktionsklip ligger ~60 s
fra hinanden -> en blok med tynd dialog gav 1 anker af 11 klip (E04 29-37 min,
479 s x 4,4 s, tavs i full mode). Ny regel: hver matchet linje fra hele det
cachede transskript (baglaens-tilbage 30 s-klip) paa filen paa disken; >= 4 af
6 linjer i traek enige (MAD <= 0,5) og >= 2,0 s fra filen. Rask SH max 1,43 s,
indlagte blokke >= 2,64 s. Efter bevist blok (resync/alass-blokfit): ogsaa 3 i
traek >= 5 s (fanger kanter; en rask SH-fil har et saadant tripel ved 37:54 i
E02 -- derfor kun ved bevist blok).
Resultat: tavse blok-raekker 42 -> 2 (E05 575 s x -2,3 s, 97,3 % rettet i
sampled). Standardmatricen: 0 klasseskift, clean/dropdup uroerte, arm 1
312/312. Pris: flere lykkedes blok-reparationer faar nu advarsel (28 -> 16
rent rettede i full) -- i traad med "blokke skal detekteres".
Fejl undervejs: `_final_full_samples` brugte kun samples naar "new" vandt; ved
Δ < 0,25 s vandt "old" og reglen blev aldrig spurgt.

## 14.33 Graenserne er modelbundne -- rigtige afsnit med turbo-transskript afsloerede det

genuine.py bruger turbo-transskripterne i out/. Med blok-/hulregler kalibreret
paa tiny flaggede den 4 korrekte SH-filer (huller 8:30/41:16/53:02 med 28-93 s
tale; SH_S01E04 22:47 -3,6 s = brugerens bekraeftede scene). Maalt paa alle 16
transskripter (15 modeller + out/):
- Huller: rask max 86 ord (turbo-q5_0), tiny 34. Barre 100 ord -> fri af alle
  modeller; skips >= 10 linjer 91-98 %, >= ~20 linjer 100 %. Taethed (ord/s)
  skiller IKKE (turbos lange segmenter) -- hypotese afkraeftet.
- Blokke: rask max pr. model 1,1-12,2 s (medium.en 12,2, medium-q5_0 9,6,
  turbo 8,1, base.en-cpu 5,5; tiny.en-greedy 1,4, base.en-greedy 1,4,
  turbo-q8_0 1,1). Ingen faelles taerskel mulig. 2,0 s gaelder hovedmodellen.
  Model-uafhaengig loesning: VAD-bekraeftelse af hver raekke (brugerens ide)
  -- eller drop de ustabile modeller (brugerens regel).
Hovedmodel paa rigtige SH med egne transskripter: 12/12 ok og uroert.

## 14.34 Blokke og skips: VAD-bekraeftelse, hul-probe og slutresultat (commit d34384e)

- Blok-raekker (>= 4 af 6 linjer enige, >= 2 s) bekraeftes nu mod VAD:
  skiftet skal passe bedre paa talen end 0 (`shift_fits_speech`). Det goer
  blokreglen modeluafhaengig (turbo-transskripter gav 8 s raske raekker).
- Fejl fundet: whisper-vad-speech-segments laeser kun WAV; paa mkv hang den i
  minutter. Nu: ikke-wav -> None, og `speech_timeline` udtraekker WAV (memo).
- Sampled: billigt VAD-vidne (`block_witness`, 60 s vinduer) koeber det fulde
  transskript ved mistanke. SH: 6/6 tavse sampled-blokke udloeser, 1/6 rene
  (kun pris).
- Huller: gap-eskalering ved 50 s koebte fuldt transskript paa de fleste SH
  (brud paa pristesten) -> erstattet af probe der kun transskriberer selve
  hullet (hele hullet, stop tidligt). Delvis probe (3 klip) missede et 300 s
  hul med 86 ord -- derfor hele hullet.
- Afkraeftet: alass splitter ikke disse blokke; VAD-resttjek efter resync
  fangede 0/4 og flaggede E06 -> fjernet.
- Resultat (tiny, SH, tilfaeldige scenarier bh9): skips >= ~20 linjer 100 % i
  begge modes; tavse blok-raekker 42 -> 4 pr. mode (delvise reparationer
  0,898-0,973). Standardmatrice: 0 klasseskift, arm 1 312/312, clean/dropdup
  uroerte. Rigtige afsnit (alle 52, turbo): 0 aendringer i de rene saet.
  Sampled Whisper-lyd -24 %. Suite 332 groen.
- Tilbage: 2 skips slipper (E01 hul 10 linjer = 95 ord, graenser op til rask
  hul med 34 ords baggrundstale; E03 afkortet slut 16 linjer = 71 ord). Barren
  100 ord er sat efter turbo-q5_0 (rask 86). Tiny topper paa 34 -> undersoeges
  om hovedmodellen kan faa egen barre (tiny-transskripter laves til alle
  Community-afsnit i sweep_linux/ for at maale raske huller).

## 14.35 Skips: tiny faar egen hul-barre (50 ord)

- tiny-transskripter lavet til alle 52 afsnit (Linux-build, samme flag som
  sweepet; C_S02E01 kontrol: 3041 mod 3055 ord) -> sweep_linux/tiny.en-greedy-cpu/.
- Rask top pr. model (SH, greedy): tiny 34, base 64, medium 61, small 72,
  turbo 68-86. Een faelles barre er umulig; 100 for alle, 50 for tiny.
- Community-"huller" paa 75-138 ord paa tiny er bekraeftede blok/drift-filer
  (C_S03E02, C_S02E08) eller ubekraeftede -- raa cue-tider, ikke rask tale.
- Alle 52 rigtige afsnit gennem roerledningen med tiny: barre 50 og 100 giver
  IDENTISK resultat (104/104 raekker). SH + swap-fri Community uroerte.
- Matricen satte aldrig local_whisper_model efter den testede model (alle
  koersler hed "tiny.en" i cache-noeglen) -> rettet, ellers ville turbo-
  transskripter faa tiny-barren.
- Resultat: skips >= 10 linjer 100 % (hul 42/42, afkortet start/slut 100 %).

## 14.36 Drift: hele raten maales paa originalfilen over det fulde transskript

- Nye scenarier: drift_rand0-5 (0,15-5 %, log-uniform, +-10 s offset),
  ratio_rand0-3 (1001/1000, 25/24, 25/23,976, begge retninger, +-10 s).
- Foer: doedt baand 0,18-0,36 % (tilt < 8 s-gaten): 12 tavse + 10 flaggede
  uden rettelse; alass giver et konstant skift og lader rampen ligge, fps-
  stien kender kun 0,1 % (rettede 0,25 % som 0,1 %).
- Maaling (taette ankre, hver matchet linje, paa filen FOER sync): raske/
  blok/hul |tilt| <= 0,4 s; 0,1-4,3 % laeser 2-92 s ved rho >= 0,74, keep >=
  0,90, gain >= 0,27, resid <= 0,32. Blokke falder paa keep (<= 0,65) eller
  gain < 0/resid 0,61. Intercept-bias kun +-0,09 s. Maalt ratio ligger
  inden for 0,06 procentpoint af den rigtige standardratio -> snap.
- Nyt trin: rate fra originalfilen (snap til 1001/1000, 25/24, 25/23,976 naar
  det passer lige saa godt), kun naar den nuvaerende fil ikke allerede er
  rask-flad, og rettelsen skal vaere flad bagefter. Sampled: ramp i poolen
  koeber det fulde transskript.
- Fejl undervejs: maalt paa presync-filen i stedet for originalen gav -5,98 %
  paa en 2 %-fil (SUSPECT); originalen gemmes nu separat (_orig_subs).
- Foerste koersel (dr2): drift 0 tavse / 0 uloeste (foer 12 + 10). Tilbage:
  5 traek med p50 0,31-0,35 s rettet af andre stier -> flad-tjek strammet.

## 14.37 Drift rettet 100 % paa hovedmodellen (commit 77f3d34)

- To-pas-maaling: originalen matcher kun, mens offset'et er inden for
  vinduet (PAL: foerste ~20 min) -> kort spaend overfitter og slaar snap
  (+4,13 % i stedet for 25/24). Anden maaling paa den grov-rettede fil,
  mappet tilbage, daekker hele filen -> snap rigtigt, p50 0,01-0,05 s.
- Snap valgte sidste kandidat inden for tolerancen, ikke den bedste -> rettet
  (test med 25/24 og 25/23,976 midt imellem, mutationstjekket).
- alass' 5-blok-trappe paa 0,36 % drift laeste "flad" (tilt/offset smaa),
  men resid 0,5 -> flad-tjekket kraever nu resid <= 0,30 (rask SH <= 0,28).
- Resultat (SH, tiny): tilfaeldig drift + rigtige ratioer 240/240 rettet;
  drift-matrice 216/216 (p50 median 0,046 s, max 0,236 s), ingen flag;
  standardmatrice 0 klasseskift, ingen raekke med daarligere p50, PAL/fps
  p50 0,11-0,33 -> 0,004-0,05 s; SH_S01E06 2 % sampled SUSPECT -> rettet.
- Rigtige afsnit (52, tiny): 0 aendringer. C_S02E11 (framerate) rettes
  stadig, C_S02E08 (anden udgave) flagges.
- Nyt fund: uniform_neg (-45 s) klemmer de foerste 45 s linjer til 0 -> hovedet
  mangler reelt efter rettelse; tiny-barren flagger det nu (korrekt).
- Suite 342 groen.

## 14.38 Blok-"rettet" var for loest maalt -- 60 tavse rester skjult

- randeval talte en blok-raekke som rettet ved >= 98 % inden for 0,5 s. Med
  "ingen linje > 2 s forkert" (dr4_rand, SH, tiny): kun 8 af 144 blok-raekker
  helt rettet, 76 flagget, 60 TAVSE med 1-58 forkerte linjer ved kanterne.
  Tidligere rapporteret 88-92 % fanget var derfor for optimistisk.
- Eksempel SH_S01E01 block_rand0: blok 64-479 s x -19,6 s. alass' 2-blok-fit
  lagde graensen forkert og flyttede ogsaa de 13 KORREKTE linjer 0-63 s med
  +19,6 s. Punkt-run fandt 3 af dem, men VAD-bekraeftelsen afviste (vinduet
  blander flyttede og korrekte linjer).
- Politik (arm 2 = detekter; ret kun hvis verificeret): enhver blokreparation
  i >= 2 dele (alass-blokke eller anker-regioner) flagges nu SUSPECT;
  reparationen bliver paa disken. Flag-pris maalt foer: i standardmatricen
  ender blokreparationer som ok KUN i piecewise-scenarierne (rigtige
  blokfejl); paa rigtige afsnit er alle 3 blokreparationer allerede SUSPECT.
- Resultat (commit, bk1): tilfaeldige blokke 72/72 flagget (0 tavse, foer 60);
  skips >= 10 linjer 88/88; clean uroert; standardmatrice: kun 22 piecewise
  ok -> SUSPECT, ingen timing aendret; rigtige afsnit 0 aendringer; suite 344.

## 14.39 Afsluttende review (agent) -- verificeret og rettet

Rapport: AFSLUTTENDE_REVIEW.md (+ afsluttende_review_profiler/). Kontrolleret i koden:
- K1: produktionen koerte small.en-q5_1 med VAD SLAAET FRA (tom vad_binary);
  aldrig testet; gav falske blok-SUSPECT paa rene SH (small, turbo). Brugerens
  beslutning: tiny.en + VAD. Rettet: compose tiny.en, vad_binary default
  /usr/local/bin/whisper-vad-speech-segments. Ekstra fund: Dockerfilen byggede
  VAD-binaeren men kopierede den aldrig til slut-imaget, og Silero-modellen blev
  ikke hentet -> nu kopieret + download-vad-model.sh silero-v5.1.2. (Image ikke
  bygget lokalt -- ingen docker her.)
- K2: line-order-cachen gemte ikke full_coverage/fps_points -> genkoersel af
  uaendret fil tabte missing middle + jitter (SUSPECT -> ok). Rettet (_cache_json),
  cache med fuld daekning genbruger full-mode evidens. Test + mutation.
- H2: _pre_sync_subs/_orig_subs laekkede til write_report i generate/bazarr-stien
  (TypeError). Rettet i apply_pending_sync. Test.
- H1: VAD afkodede hele lydsporet igen pr. fil (80-97 % af raekketiden).
  Rettet: alass' WAV genbruges (KNOWN_WAVS, kun hvis nyere end videoen), fejl
  huskes ikke laengere, temp-stier memoiseres ikke. Lyd-timeout 180 -> 600 s.
- Nyt fund: VAD til sample-placering (timeline_for_video) har ALDRIG koert paa
  video -- run_vad_timeline tager kun WAV. Ikke aendret (ville flytte alle
  sampled-resultater); aaben.
- Aabne fra reviewet: gap-probe +249 s lyd pr. rask sampled-fil, transskript
  filtreres 6-10 gange pr. fil, Bazarr-hook uden job-laas, 8 doede funktioner,
  correctness_and_finish 588 linjer, test-mkdtemp uden oprydning (3,7 GB /tmp).
- Commits: 9501266 (K1/K2/H1/H2 + timeout), 1ecafd4 (tests rydder mkdtemp op;
  4 GB testrester slettet i /tmp), 5e43c31 (8 doede funktioner + 12 ubrugte
  importer). Suite 350 groen. Matricen er ~3x hurtigere (WAV-genbrug).
- Afkraeftet optimering: VAD-styret gap-probe. VAD daekker kun 57-71 % af
  cue-taletiden paa SH; indlagte huller mistede op til 83 ord (E01 95 -> 37).
  Detektion vejer tungere -> gap-proben beholder sin pris (~250 s tiny-lyd pr.
  rask sampled-fil). Caching af probe-klip i klip-cachen fravalgt: de ville
  indgaa i kandidat-evidensen ved genscanning.

## 14.40 Review runde 2 (alle aabne punkter)

- VAD til sample-placering paa video: maalt i stedet for antaget. Sampled
  matrice med VAD-placering: 0 klasseskift, p50 12 bedre / 12 vaerre (def),
  8/7 (rand). Ingen gevinst for en ekstra lydafkodning foer alass -> beholdt
  dialogtaethed, dokumenteret i vad.timeline_for_video.
- Bazarr-hook race: flock paa /data/run.lock i jobs.execute_run (alle koersler);
  CLI single venter max 15 min og springer saa over. Test + mutation. (064203d)
- Transskript filtreres een gang pr. fil (memo), ikke 4-6 gange. Fejl
  undervejs: sammenlaegningen fjernede `model` i _missing_middle_hit ->
  NameError i alle raekker; suiten (25 fejl) fangede det foer commit.
  Laere: ruff F821 efter hver mekanisk sammenlaegning.
- Blokflag ser nu ogsaa alass' direkte multi-blok-skrivning; blokfelter
  nulstilles efter rate-rettelse; second look i rate-trinnet gated. (663e1dd)
- Jitter: alle 24 flagges; 4 afsnit forbedres, E03/E04 forvaerres (p50 2,0 ->
  2,2/3,1). Tilbagerulning ville forvaerre de 4 -> uaendret, filen sendes til
  hent-ny alligevel.
- uniform_p03 (+0,3 s) stod som "maa ikke flyttes" i harnessen -> 20/24
  korrekte flytninger talt som fejl. Rettet (39226f9).
- Suite 11 -> 6 min (screen-order-tests genbruger staging-WAV).
- correctness_and_finish 588 -> 454 linjer: _gather_evidence (eskaleringsstige
  som ordnet liste), _detection_note (sidste detektorer i fast raekkefoelge,
  testet), act_on_suspect (10 ens kald).
- Aabent: overfitting-tabellen kan ikke valideres held-out paa injicerede fejl
  uden flere bekraeftede baser (reglen: kun SH). Rigtige afsnit paa tiny (52)
  er held-out for FP: 0 aendringer gennem hele runden.
- Opdeling committet: identisk output (852 matrixraekker + 104 rigtige). Suite 357.

### 14.41 /simplify-gennemgang (4 agenter: genbrug, forenkling, effektivitet, hoejde) -- 03943fc
- Rettet uden adfaerdsaendring: doede screen-tjek (10 s + afsluttende residual,
  begge daekket af 2,5 s-tjekket), faelles VAD-maske + prefix-sum-score (block_witness
  ~6x hurtigere; 1200 tilfaeldige + 3000 overlap-tilfaelde identiske), dobbelt VAD-memo,
  KNOWN_WAVS ryddes naar sweepets tempdir lukker, ffprobe-varighed memo'et (3-6 kald
  pr. fil -> 1), bisect i dense_anchor_points, Theil-Sen sorterer een gang,
  faelles missing-middle-barre, laasepolitik fra CLI i stedet for trigger-streng.
- Valideret: matricer 300 + 552 raekker, 52 rigtige afsnit (104) og suite 357:
  identisk med rf1.
- Fejl undervejs: `wait_s` stod tilbage i en logline -> NameError; test fangede det.
  Lint blev koert efter testen -- skal foer.
- IKKE rettet, men reelt (effektivitet): hver sampled fil dekoder hele lyden til
  VAD i stigen (33 s wall pr. SH-afsnit her, 30-100 s paa N100), ogsaa screen-ok
  filer; hook-stien dekoder to gange (ingen audio_cache). Kandidat: VAD-tidslinje
  i DB pr. (sti, mtime, stoerrelse). Gap-proben (~4,6 min Whisper-lyd pr. afsnit)
  caches ikke paa tvaers af koersler. Begge kraever nyt DB-felt/ny cache -> egen opgave.
- Afvist: spring gap-vinduer over med VAD < 3 s -- allerede maalt (14.3x): VAD
  daekker kun 57-71 % af talen, huller tabte op til 83 ord.
- Udskudt (stoerre omskrivninger): SyncOutcome i stedet for private row-noegler,
  een rate-fixer i stedet for tre, kalibreringsprofil pr. model, skema-version paa
  cache-noeglen, antal reparationsdele som felt i stedet for regex paa status.
- Opfoelgning: 10 af de sprungne fund implementeret som hver sin commit
  (2bdfd58..1f27bc9), IKKE testet endnu. Plan, grunde til resten og testraekkefoelge:
  rapport/PLAN_SIMPLIFY_RESTER.md. Rate-stien har `sync.rate_legacy_paths` som vej tilbage.
- Test af opfoelgningen: punkt 9 (een rate-sti) fejlede -- rate-stien fra originalen
  kan ikke se 0,1 % paa korte afsnit (C_S02E11: ~1,3 s tilt < 1,5 s-gulvet); den
  diskrete 0,1 %-sti er baerende og bliver. Rullet tilbage (27bd6bc). Resten: 0
  forskelle i matricer/rigtige afsnit; genkoersel sparer hele VAD-dekodningen og
  gap-probe-lyden (699 s -> 17 s frisk Whisper-lyd paa SH_S01E01).

### 14.42 Known Good (5 brugerbekraeftede afsnit) og klip-taethed -- 2026-09-27
- Known Good uroert paa tiny+VAD: 4/5 ok; Breaking Bad flaggede en sang (55:01, tiny
  skrev 60 ords sangtekst uden noder). Rettet (576efd7): tekst med noder og i klammer
  taeller aldrig (brugerens regel), og et hul som underteksten selv aabner med en
  musikbeskrivelse (<= 240 s) er en sang. Nu 5/5 ok. SH-matricer + 52 rigtige: 0
  flagaendringer. Maalt: ingen Whisper-stoerrelse markerer sangtekst paalideligt
  (medium.en 76 %, tiny 39-57 %, turbo 0-10 %).
- Indlagte fejl paa Known Good: forskydning/framerate/blokke 100 %. Fundet: veto af
  alass' fit returnerede foer rate-trinnet -> 4,6 % drift i sampled uloest (KG_BMS).
  Rettet (7ebc17b). To "missede huller" var 5-6 dialoglinjer + lydbeskrivelser
  (harness talte [GUNSHOTS] som linjer) -> harness taeller nu dialoglinjer (b292530).
- Klip-taethed (gren `density`, e559733, IKKE flettet): 9 afsnit >= 40 min, sampled.
  1,3 / 2 / 3 klip pr. 10 min / faste 16 (2,7-3,8 pr. 10 min): identiske paa alle
  typer undtagen 2 stille blokke i 1,3/2/3-armene, som 16 fanger. Aarsag er placering,
  ikke taethed: med 14 klip laeser skaermen tilt 0,25 s (grænsen) i stedet for 0,07 s,
  sender filen til alass, og en blok paa 0:24-2:57 (-16 s) bliver aldrig kigget efter.
  -> Et minimum pr. 10 min giver intet paa afsnit; film (> 80 min) er ikke maalt (ingen
  film i testsaettet). Reel svaghed: sampled-blokke afhaenger af at et klip lander i
  blokken; VAD-vidnet fangede ikke disse to.
- RETTELSE til taethedstesten ovenfor (maalefejl, fundet efter brugerens tvivl):
  armene satte sample_count=1 for at styre klip-antallet, men sample_count styrer
  ogsaa full-evidensen efter eskalering (16 x 5 punkter) -> de tynde arme fik 21-28
  punkter i stedet for 78-82 efter eskalering. Konklusionen "placering, ikke taethed"
  byggede paa det. Omkoert med kun sampled-klip styret (--exact-per-10, sample_count
  16): 1,3 / 2 / 3 / 4 pr. 10 min og faste 16 er IDENTISKE paa alle typer undtagen
  een blok (SH_S01E04 block_rand3, 29,6-170,8 s, +16,4 s), som kun 16 fanger -- baade
  faerre og flere klip misser den. Ingen maalbar taethedseffekt paa 42-59 min.
  Desuden: analysescriptet talte et "hul" med 0 fjernede linjer som stille miss
  (`or 99`) -- rettet; huller er 100 % i alle arme.
  Reel svaghed: blok i de foerste ~3 min i sampled fanges kun hvis et klip rammer den.

### 14.43 2 klip pr. 10 min som standard (baaa288) -- validering 2026-09-27
- 11 bekraeftede baser (6 SH + 5 KG), sampled 16 faste mod 2 pr. 10 min (min. 3):
  forskydning/drift/huller/rask uroert/forkert afsnit uaendret. Tab paa de korte
  afsnit (KG_BOB 22 min = 5 klip, KG_BMS 29 min = 6 klip): 2 framerate (BOB 0,1 %)
  og 3 tidlige blokke (BMS) + SH_S01E04 block_rand3. Full-mode: alt 100 % undtagen
  reklamescenariet (se nedenfor).
- Framerate-tabet var en KODEFEJL, ikke taethed: naar alass' forslag blev afvist,
  erstattede afvisningen det fulde transskripts evidens med de faa sampled-klip, og
  den diskrete 0,1 %-sti kraever fuld daekning; i veto-grenen blev den slet ikke
  proevet (kun rate-fra-originalen med 1,5 s-gulv; 0,1 % paa 22 min = 1,3 s).
  Rettet: faelles rate_fixes() i begge grene; sparsom evidens genlaeses mod det
  cachede fulde transskript (ingen ny Whisper). BOB fps_early/fps_late: fixed.
- Nyt scenarie "reklameklip" (cutsteps: 1-3 klip, alt efter flyttes, summeres):
  slap igennem i ALLE arme inkl. full (KG_BMS -81,7 s fra 21:35; SH_S01E03 +17,8 s
  fra 4:06, "rettet" som een forskydning -> de foerste 4 min forkerte, flag ok).
  Aarsag: ankrene soeger kun 30 s tilbage, saa > 30 s forskudt tekst er usynlig for
  dem; indholdsscoren viser det tydeligt (11 punkter i traek 0-0,44 mod 0,75-1,0),
  men dommen er "flertal over taerskel" og overstemmes af resten af filen.
  Under arbejde: detektor for sammenhaengende lav score hvor underteksten HAR tale
  (sange i raske filer har lav score men faa undertekst-ord). Kalibreres paa SH+KG.
- Suite: 1 roed (test_screen_order): rask SH_S01E01 faar tilt 0,45 s paa 11 klip
  (rho 0,39, gain 0,06 = stoej) og gaar til alass -- resultat rigtigt, men koster en
  fuld lyddekodning. Skaermens tilt-gate maales paa drift vs raske foer aendring.
- Reklameklip, fundet: blokdetektoren (_block_runs_hit) saa kun linjer 30 s tilbage /
  60 s frem -> BLOCK_RUN_REACH_S = 180. Maalt offline paa de indlagte filer: 110/110
  reklame- og 44/44 blokfiler giver en raekke (foer 62/110); raske SH+KG top uaendret
  (0,65-2,0 s, 0 raekker). Anden fejl bag det: VAD-bekraeftelsen valgte undertekster
  efter LYD-tiden, men de forskudte linjer ligger dev s vaek i filen -> ved 82 s testede
  VAD de forkerte linjer og afviste. Raekken baerer nu sine egne cue-tider.
  KG_BMS -81,7 s, SH_S01E03 +17,8 s, KG_BB -105,8 s: alle SUSPECT nu (full).
- Tidlige isolerede blokke paa korte afsnit i sampled (KG_BMS block_rand0/2/3,
  SH_S01E04 block_rand3): intet klip lander i blokken og VAD-vidnet kan ikke skelne
  paa BMS (ren fil giver +-30 s-vinduer med gain 0,3-0,9). Strukturel graense for
  sampled -- kraever flere klip eller fuldt transskript. Brugerens beslutning.
- Skaerm-omkostning ved 2 pr. 10 min (maalt, 11 baser, alle scenarier): rask fil
  bestaar skaermen 5/22 (foer 16/22) -- stoej-tilt op til 0,95 s (rho -0,67) paa 6-11
  klip. Loesnes tilt-graensen til >= 0,5 s, slipper +0,3 s forskydning igennem paa 4/11
  -> skaermen uaendret. Konsekvens: de fleste raske filer koerer nu alass (een fuld
  lyddekodning), resultatet er stadig rigtigt.
- Validering efter 4ad6f56 (11 baser, alle scenarier): full 220/220 blokke (foer
  217), sampled 216/220 (de 4 tidlige isolerede blokke), alt andet 100 %. 52 rigtige
  afsnit: 0 forskelle mod foer rettelserne (bredt vindue giver ingen nye alarmer).
- ARM 1-TAB PAA RIGTIGT AFSNIT ved 2 pr. 10 min: C_S02E11 (brugerbekraeftet 0,1 %
  drift, 21 min) rettes med 16 klip, IKKE med 5. Framerate-udloeseren faar 33
  ankerpunkter mod 107 og dens tre tilt-tests (hel/binned/leave-one-out) fejler.
  Klip-sweep paa afsnittet: 7 klip nej, 9+ klip ja (eet afsnit -- ikke nok til at
  saette en graense). Suite: test_screen_order roed af samme grund (skaerm-stoej).
  Afventer brugerens beslutning om minimum for korte afsnit.

### 14.44 Klip-placering, start/slut-drift og tekst efter lydens slutning -- 2026-09-27
- Klip placeres allerede efter tale (taetteste dialog + VAD). Men et omraade med en
  mistaenkt byttet linje fik et KORT linjetjek-klip i stedet for timingklippet: 1-2
  ankerpunkter. Community med 5 pladser: 2-4 gik til linjetjek. Rettet: hvert omraade
  beholder altid sit timingklip, linjetjek laegges oveni (CACHE_SCHEMA 3). Drift-
  afsnittene faar nu ankre i begge ender (C_S02E06 1/5 -> 5/5, C_S03E04 2/5 -> 4/5).
- Framerate-udloeserens "binned"-test kraever punkter i 8 af 16 tidsbaand -> kan ikke
  bestaas med < 8 timingklip (afsnit < 40 min ved 2 pr. 10 min). Klip-median-haeldning
  (brugerens start/slut-ide) med ny placering: rigtig drift 0,83-1,69 s, bekraeftet
  raske op til 0,90 s (KG_BB) -> overlap paa C_S02E11 (0,83). Ingen ren graense endnu.
- Brugerens regel: mange byttede linjer -> marker og hent ny; ankre er ustabile der.
  C_S02E06/C_S03E04 (drift + mange swaps) ender korrekt som SUSPECT "hent ny".
- Tekst efter lydens slutning (brugerens ide), maalt: 11/11 bekraeftet raske slutter
  1-78 s FOER lyden. 7 Community-afsnit har tale efter slutningen (1-29 linjer, op til
  +122 s) -- alle allerede SUSPECT, og hvor facit findes er de forkerte (C_S02E08 anden
  udgave +1,5 min, C_S02E16/17 tekst matcher ikke, C_S03E02 blokke). Reklamescenariet:
  ~50/110 overskrider (dem hvor klippene tilsammen flytter mere end luften efter sidste
  replik). Bygget som rent "noget er galt"-signal, ingen aarsag udledes: skaermen maa
  ikke kalde filen rask, sampled eskalerer, og en faerdig fil med tale efter slutningen
  bliver SUSPECT. Tekst med noder/klammer taeller ikke.
- Brugerens regel (remediate): finder Bazarr ingen korrekt undertekst inden for
  automation.remediate_max_attempts, beholdes originalen og markeres forkert. Foer: Bazarr
  slettede originalen ved sortlistning, og sproget blev efterladt som manglende. Nu:
  indholdet holdes i hukommelsen foer sortliste/karantaene og laegges tilbage, naar ingen
  kandidat bestaar ("original kept, marked wrong"); raekken forbliver SUSPECT. update_state
  koerer efter handlingen, saa den tilbagelagte fil ses som uaendret -> ingen ny runde
  forsoeg ved naeste koersel. Test: tests/test_remediate_keep.py.
- Validering v3 (ny placering + tekst efter slutningen): full uaendret 100 % / 220/220;
  sampled nyt tab: KG_BOB trunc_start_rand1 fik en FORKERT rate-stretch -4,09 % skrevet.
  Aarsag (gammel fejl, afsloeret af nye klip): _reject_unproven_old afviste originalen
  (ingen anker i alass' blok over det fjernede stykke) og faldt tilbage til 'new', som
  var klart daarligere paa baade tekst (0,75 mod 0,87) og ankre (13,9 s mod 0,1 s);
  vetoet kraevede tekst-uafgjort. Rettet: vetoet gaelder ogsaa naar originalens tekst er
  lige saa god ELLER bedre (ankre overstemmer stadig aldrig bedre tekst hos vinderen).
  -> originalen beholdes, SUSPECT.
- test_drift_swap_sampled: rettet korrekt (rec 1,00), men nu via skaermens pre-sync
  (-1,96 %, 29 ankre) i stedet for stretch-stien -- flere timingklip. Test godtager nu
  begge veje.
- Tekst efter slutningen, maalt i matricen: 0 raske filer; paa indlagte fejl: cutsteps
  47/110, drift 13, PAL 11, ratio 16, forkert afsnit 4, forskydning 1. Efter rettelse
  stadig over: 6 cutsteps + 4 forkert afsnit + 1-2 stoej -- alle SUSPECT. Ingen nye fund
  i testsaettet, ingen falske alarmer.
- 52 rigtige afsnit v2->v3: 0 aendringer i flag/sync/omskrivning.
- Start/slut-drift som udloeser (2f166d7): klip-median-haeldning >= 0,7 s over >= 3 klip
  der daekker >= 60 % koeber fuldt transskript (retter aldrig selv). C_S02E11 rettes nu
  i sampled; 52 rigtige afsnit ellers uaendret (SUSPECT 72 -> 72); matrix uaendret,
  +0,7 min lyd/fil. Indstilling sync.clip_tilt_escalate_s.
- Frontend (5578cfd): Simple/Advanced. Fundet: sync.escalate_min_bad_samples laeses
  ingen steder (doed indstilling); line_order_audio_confirm og swap-taersklerne aendrer
  kun notens ordlyd -- "mange byttede linjer"-markeringen koerer altid med faste
  graenser (10 %, 5 bekraeftede). Begge fjernet fra UI.
- Aarsagskode pr. markeret fil (files.reason) til UI'et. Matrix: 0 aendringer i flag/sync,
  alle SUSPECT har en kode; huller/afkortning -> missing_lines, blokke/stykkevis/reklame ->
  partly_out_of_sync, forkert afsnit -> wrong_subtitle, stoej -> unreliable_timing (8/11;
  3 markeres via alass' blokforslag og hedder partly_out_of_sync). 52 rigtige: 24
  lines_out_of_order (Community-swaps, brugerens regel vinder), 8 wrong_subtitle, 4 partly.
  Fundet af test: filter "other" tog raske filer med -- rettet.

## Ultrareview af reason-koder (2026-09-28)

- Review-rettelsen f57b9b6 (anden session) ændrede reason efter anchor-resync (`block_shaped`). Målt på jitter-scenariet (11 baser, sampled, samme flag som cal_v7s): 0 ændringer i flag/reason/sync. Kun jitter havde `unreliable_timing` i referencen, så ingen andre rækker kan flytte.
- Migreringer i `db.connect` slugte alle `OperationalError` — en låst database under opgradering efterlod kolonnen manglende i stilhed, og hver senere skrivning fejlede. Rettet: én tabel `_ADDED_COLUMNS` + `_add_column`, der tjekker `PRAGMA table_info` og lader andre fejl rejse. Verificeret: gammel database (24 kolonner fjernet) migreres til identisk skema med gammel og ny kode; låst database rejser nu.
- Files-siden ignorerede `?reason=` — både listen og "vælg alle" tog alle filer. Rettet med reason-filter i dropdown.
- Ukendt `reason` gav tom liste med 200; giver nu 422.
- Stadig åbne (kræver beslutning): remediation overskrives af sidste update_state; "Needs attention" tæller filer i karantæne/slettede; gamle SUSPECT uden reason; `unknown` tælles ikke; reason-kategorier for aldrig-synkroniseret rigtig episode, veto-grenen og hel-fil-forskydning.
- Remediation overskrevet (bekræftet): `remediate_suspect` gemmer erstatningens ok-række, derefter skrev `correctness_and_finish` den gamle SUSPECT+reason over den (samme `subtitle_path`-nøgle) — filen stod i "Needs attention" for evigt. Rettet: efter godkendt erstatning gemmes kun `auto_action` på rækken. Ny test kører rigtig pipeline (SH_S01E01 wrong_episode sampled); fejler uden rettelsen, består med. Suite 367 bestået.
- `unknown` fik altid en grund: `no_speech_heard` (kun VAD-stilhed/ingen tale i alle klip) eller `check_failed` (teknisk fejl). Tælles nu i "Needs attention". Målt: 0 unknown i 605 matrix- og 104 rigtige rækker — sker kun ved fejl.
- Drift under blok-trappe (SH_S01E05 drift_rand5, −0,285 %): alass gav 5 blokke, `_try_rate_from_baseline` læste raten korrekt (tilt 6,5 s, rho 0,96) men sprang over, fordi trappen læser flad (tilt 0,0). Resultat var SUSPECT partly_out_of_sync, 89 % ≤0,5 s. Rettet: flad-tjekket springes over når filen er en blok-fit (≥2 blokke); "flad efter"-vagten står. Nu: rate +0,27 %, ok. Matrix 605 rækker: 1 diff (målcellen). Rigtige afsnit 104 rækker: 0 diffs, SH 0 omskrevet/0 flaget. Test fejler uden rettelsen. Suite 369 bestået.
- De 5 øvrige arm-1 SUSPECT (uniform_neg) er korrekte: −45 s rettet 100 %, men scenariet sletter 11–17 linjer før 0:00 → missing_lines er sand.

## Frontend efter design (2026-09-28)

- Muse byggede alle 12 skærme; kun rigtige data. Testet i Chromium (Playwright) på en seedet DB med de 52 rigtige afsnits statusser: 27 skærme desktop + mobil, 0 konsolfejl, 0 fejlede API-kald efter rettelser.
- Fundet i browseren (ikke af tsc/build): uendelig render-løkke på alle Settings-faner (effekt afhang af et kontekstobjekt der er nyt hver render). Rettet.
- "Recently fixed"/"Fixed"-filter viste flaget filer med grønt ✓ (sync flyttet, men SUSPECT) — nu kun flag ok. Match rate-graf klemte 30 % op til 75 %-bunden — aksen følger nu data. Swap-"flagged" rødt på raske filer — nu dæmpet "unsure", rødt kun ved lines_out_of_order.
- Scheduler: cron-dag 0 fyrede mandag (APScheduler tæller fra mandag) — rettet til søndag. Muse låste triggeren til UTC, hvilket ville flytte alle eksisterende scanninger 2 t; triggeren kørte faktisk i lokal tid (TZ), det var UI-teksten "(UTC)" der løj — låsning fjernet, tekst rettet.

## Transskriptionsnøjagtighed pr. model (2026-09-29)

Målt: hver models sweep-transskript mod de bekræftet rigtige Slow Horses-undertekster (S01E01–E06, `model_quality.py`). Ord i rækkefølge (difflib), ♪ og klammer tæller ikke. Recall = andel af undertekstens ord modellen fandt; precision = andel af modellens ord der står i underteksten.

| model | recall | prec | F1 | loop-seg |
|---|---|---|---|---|
| medium.en-greedy | 0,878 | 0,907 | **0,893** | 10 |
| medium.en-q5_0 (kun 5 af 6, E03 crasher) | 0,883 | 0,892 | 0,887 | 17 |
| turbo-q5_0 | 0,895 | 0,879 | 0,887 | 61 |
| medium.en | 0,882 | 0,889 | 0,886 | 5 |
| turbo-q8_0 | 0,891 | 0,871 | 0,881 | 98 |
| small.en-greedy | 0,854 | 0,890 | 0,872 | 21 |
| small.en-q5_1 | 0,862 | 0,880 | 0,871 | 31 |
| small.en | 0,862 | 0,880 | 0,871 | 44 |
| turbo (cloud) | 0,840 | 0,796 | 0,818 | 418 |
| base.en (3 varianter) | 0,80–0,81 | 0,82 | 0,81–0,82 | 47–56 |
| tiny.en-cpu | 0,750 | 0,767 | 0,758 | 68 |
| tiny.en-q5_1-cpu | 0,743 | 0,768 | 0,755 | 36 |
| **tiny.en-greedy-cpu** (produktion) | 0,737 | 0,756 | **0,747** | 58 |

- tiny.en er den **dårligste** af alle 15 på ord: F1 0,75 mod 0,87 (small) og 0,89 (medium). Ca. 12–14 point bagud; laveste episode 0,69.
- Forbehold: underteksten er ikke en ordret nedskrivning, så absolutte tal er lavere end den rigtige nøjagtighed. Rangordenen er retfærdig (samme facit for alle). medium.en-q5_0 mangler E03 (DTW-crash, 3.1). Loop-tal er før appens loop-fjernelse.
- Modsiger ikke tidligere målinger: detektionsmatricen (B2) lå 0,77–0,82 på tværs af modeller — bedre ord har ikke vist sig i detektionen. Målt ord-nøjagtighed og detektionsevne er to forskellige ting.
- Modelvalget hviler derfor på hastighed (16 klip: 9 s tiny, 48 s small.en-q5_1) og kalibrering, ikke på transskriptionskvalitet. Åbent (brugerens beslutning): om +12 F1-point ord er værd ~5× tiden. Test der kan afgøre det: detektion på svære tilfælde (blokke på tynd dialog, lille drift) med small.en-greedy mod tiny.en, ikke flere ordtal.

### Ankre pr. model (2026-09-29, `model_anchors.py`)

Spørgsmål: giver dårligere ord færre/dårligere ankre? Målt med produktionens matcher (`dense_anchor_points`, 30 s-klip over hele transskriptet) på de godkendte afsnit: SH E01–E06 og Community C_S03E03/E08/E10. "Inden for 0,5 s" = ankerets forskydning ligger ≤0,5 s fra filens median (facit er rigtigt timet, så resten er falske ankre og naturlig jitter).

| model | SH ankre/10 min | SH klip ≥3 ankre | SH ≤0,5 s | C ankre/10 min | C klip ≥3 | C ≤0,5 s |
|---|---|---|---|---|---|---|
| tiny.en-greedy-cpu | 53,7 | 0,62 | 0,652 | 98,3 | 0,85 | 0,707 |
| tiny.en-cpu | 55,3 | 0,64 | 0,681 | 97,9 | 0,86 | 0,739 |
| small.en-greedy | 67,6 | 0,72 | 0,679 | 109,7 | 0,88 | 0,663 |
| small.en | 67,0 | 0,70 | 0,669 | 108,1 | 0,90 | 0,708 |
| medium.en-greedy | 72,2 | 0,74 | 0,700 | 109,5 | 0,85 | 0,680 |
| base.en-cpu | 62,5 | 0,68 | 0,709 | 100,1 | 0,85 | 0,717 |

- **Antal:** tiny giver ca. 20 % færre ankre end small/medium på Slow Horses (54 mod 67–72 pr. 10 min), og færre klip når ANCHOR_MIN_COUNT=3 (0,62 mod 0,70–0,74). På Community er forskellen lille (98 mod 108–110; klip 0,85 mod 0,88–0,90).
- **Kvalitet:** ingen forskel. Andelen inden for 0,5 s ligger 0,65–0,75 for alle modeller uden rangorden; tiny er ikke værre (Community: tiny 0,71–0,75, small 0,66–0,71). De dårligere ord giver altså færre ankre, ikke flere falske.
- Konsekvens: tiny taber dækning, ikke troværdighed. Det kan koste på tynd dialog, hvor et klip lige akkurat når 3 ankre. Ikke afgjort — kræver detektionstest på svære tilfælde (small.en-greedy mod tiny.en).
## Modelvalg: detektion pr. model (2026-09-29)

Fuldt: `rapport/modelvalg_detektion.md`. Matrix: d85517e, 15 modeller × 10 afsnit ×
23 scenarier × full+sampled, audio-confirm off — 6900 rækker (6854 ok, 0 errors,
46 skipped: kun kendte medium.en-q5_0/SH_S01E03).

- Community-timingceller er blokerede: alle 15 modeller svarer identisk `left
  unchanged (many swapped lines)` på C_S03E03/E08/E10 — porten, ikke synckvalitet.
  Informative tal er SH-only.
- SH sampled (produktion): offset rettet 35-36/36 og rate 36/36 for tiny/small/
  medium-greedy og alle øvrige sweep-modeller (eneste misses: uniform_p03-grænsen,
  spredte enkeltrækker = støj). Blokke detekteret 40/40 og missing_middle 10/10
  for alle 15 i begge modes. wrong_episode/swap/drift_swap/dropdup/jitter:
  ingen modelforskel.
- clean: alle flagger de 4 Community (uberørte, facit-egenskab). SH clean-FP:
  0 for tiny/small/medium-greedy; medium.en-q5_0 E01 (kendt 14.24), tiny.en-cpu
  E02+E04 (gap-detektor) — alle uberørte. Eneste omskrivning af korrekt fil i
  6854 rækker: turbo full E01 (kendt 14.26, -3,7 s-artefakt). turbo misser også
  E06-drift (alass-katastrofe).
- Kryds tiny vs small/medium: 3 uenige sampled-rækker af 230 (tiny 2-1 foran),
  2 full (begge tiny foran) — støj. Eskalering sampled: tiny 79/230, small 110,
  medium 94 — tiny eskalerer mindst på lette filer med samme udfald.
- Konklusion: F1 0,75 vs 0,87-0,89 og ~20 % færre ankre slår ikke igennem i et
  eneste detektionsmål. Valget står mellem hastighed og ord-nøjagtighed;
  detektionen favoriserer ingen. Ingen anbefaling.

### Samlet grundlag for modelvalget (2026-09-29) — tiny.en vs small/medium

| mål | tiny.en-greedy (prod) | small.en | medium.en | kilde |
|---|---|---|---|---|
| ord-F1 mod rigtige undertekster (SH) | 0,747 | 0,871–0,872 | 0,886–0,893 | `model_quality.py` |
| ankre pr. 10 min (SH / Community) | 54 / 98 | 67 / 108 | 71–72 / 102–110 | `model_anchors.py` |
| klip med ≥3 ankre (SH) | 62 % | 70–72 % | 72–74 % | `model_anchors.py` |
| andel ankre ≤0,5 s fra median | 0,65 / 0,71 | 0,67–0,68 / 0,66–0,71 | 0,69–0,70 / 0,68–0,75 | `model_anchors.py` |
| offset rettet, SH sampled (n=36) | 35 | 35 | 36 | matrix 6900 rækker |
| rate/PAL/drift rettet (n=36) | 36 | 36 | 36 | matrix |
| blokke detekteret / missing_middle | 40/40 · 10/10 | 40/40 · 10/10 | 40/40 · 10/10 | matrix |
| falske positive på korrekte SH-filer | 0 | 0 | 0 | matrix |
| 16 klip, tid | 9 s | 48 s (q5_1) | — | 11.2 |

- Dårligere ord og ~20 % færre ankre på SH viser sig **ikke** i noget detektions- eller rettelsesmål. Forskellene (1 række offset, 3 uenige rækker af 230) er støj.
- **Begrænsning, skal med i enhver konklusion:** alle Community-timingceller er blokeret af line-order-porten (byttede linjer) for alle 15 modeller, så matricen måler kun Slow Horses (tæt dialog). Om tiny taber på tynd dialog, hvor et klip lige akkurat rammer 3 ankre, er **ikke målt** — det er det åbne hul, ikke afkræftet.
- Gamle kørsler (17/9 og sh_all15 24/9) er ikke sammenlignet række for række; den nye er på HEAD d85517e og erstatter dem for drift. Hullerne (hole_rand/trunc) indgår ikke i standardsættet og er ikke kørt.
- Valget står derfor mellem hastighed (tiny) og ord-nøjagtighed (small/medium); detektionen favoriserer ingen på det målte sæt. Beslutning ligger hos brugeren (jf. prod = tiny.en + VAD, 26/9).

### Rettelse til modelvalg-målingerne (2026-09-29): kun godkendte afsnit tæller

`testsaet.json` (regel: kun godkendte afsnit uden swap) godkender som baser kun **SH_S01E01–E06**. Matricen kørte også C_S03E03, C_S03E08, C_S03E10 og C_S03E04, som står under `med_swap_udelukket` — de har byttede linjer/drift og skulle ikke have været med. Konsekvens:
- Matricen: de 4 afsnit er blokeret af swap-porten for alle modeller og bidrager ikke til noget timingtal; alle detektions-/rettelsestal ovenfor er **SH-only** og gælder uændret. Deres 'clean flagget'-rækker (Community) er ikke falske positive og tælles ikke.
- Ordnøjagtighed (`model_quality.py`): kun SH — uberørt.
- Ankre (`model_anchors.py`): Community-kolonnerne (C ankre/10 min, C klip ≥3, C ≤0,5 s) er målt på ikke-godkendte afsnit og **skal ikke bruges**; kun SH-kolonnerne står.
- Tynd dialog forbliver umålt: der findes ingen godkendt tynd-dialog-base.

## Modelvalg på godkendte afsnit (2026-09-30) — erstatter tidligere modelvalg-afsnit

Fuld rapport: `rapport/modelvalg_godkendte.md`. Kun SH_S01E01–06 + 5 Known Good (BB, BIL, BMS, BOB, EUP); 14 lokale modeller Whisper-kørt på CPU på de 5 KG (65 filer, 4 tråde) + Groq whisper-large-v3-turbo (cloud) på alle 11. Matrix 8096 rækker (HEAD d85517e).
- Rettelse: den gamle `turbo` var for SH en lokal small.en-q5_1-fixture, ikke turbo — udeladt. "cloud" = Groq/OpenRouter/OpenAI; alt andet er "lokal".
- Detektion/rettelser: ingen forskel mellem lokale modeller (±1–3 rækker af ~240). Tiny.en-greedy (prod) 131/132 SH, 108/110 KG (sampled) — som small/medium. Fejl: jitter (modeluafhængig, alass) og grænsecellen uniform_p03.
- Ord-F1 (SH/KG): tiny 0,75/0,81; small 0,87/0,89; medium 0,89/0,91; cloud-turbo 0,88/0,89. Ankre: tiny ~20 % færre på SH, ikke dårligere.
- Cloud Groq turbo: ikke bedre til detektion; SH 123/132 (7 af 9 fejl = SH_S01E05 rate-rest 0,254–0,268 s vs bar 0,25 s, segment-tider uden ord-tider); loop-fjernelse ramte 6 af 11 afsnit.
- Hastighed alene, 4 tråde: tiny 25×, small 4,5–5,8×, medium 1,8–2,1×, lokal turbo 1,3–1,7×; RAM 0,6–2,7 GB.
- Konklusion: modelvalget ændrer ikke detektionen; det står mellem hastighed (tiny) og ord (small/medium). Ingen model anbefalet; beslutning hos brugeren.

## Known sync problem: Brooklyn Nine-Nine S01E03 (2026-09-30)

Kørt gennem Docker-imaget (verifyarr:new, prod: tiny.en + VAD, sampled) på en kopi. Fejl: drift — undertekst løber 1,5 s foran ved start og 2,8–3,0 s ved slut (median −2,33 s mod en fuld transskription). App: "fixed (Δ2,9 s)", 2-bloks fit valgt af verifikationen mod enkelt-offset og original (rest 0,3 s mod 2,4 s). Efter rettelse median −0,04 s, ±0,4 s over hele afsnittet; alle 285 ankre. Kun tider ændret (615 cues; 2 cues mistede blot et efterstillet mellemrum). Filerne til manuelt tjek: `docker_test/known_sync_problem/*.RETTET.srt` / `.ORIGINAL.srt`. Stadig flagget SUSPECT missing_lines: undertekst slutter 21:12, lyd 21:40 (25 s uden linjer, 19 s tale) — kan ikke rettes, hent ny.

## 10 tilfældige afsnit fra Z:\shows, tør kørsel (2026-09-30)
Liste og detaljer: `docker_test/z10_resultat.md`. Fund: filer med flere lydspor (fransk først, engelsk nr. 2: Resident Alien, Lincoln Lawyer, Severance) — appen læser kun første spor (`detect_audio_language_ffprobe` a:0) og extraherer uden `-map`; alass synker mod den franske dub (ville flytte 163,5 s / 29,6 s), korrekthedstjekket springes over. Reel risiko for at ødelægge gode undertekster. Skal rettes med lydspor-valg efter undertekstsprog. Ellers: 1 mangler linjer (Shameless), 1 delvis forskudt (Eureka), 1 forkert undertekst (Futurama), 2 ok, 1 lille rettelse 0,7 s.

## Shameless S05E06 – sang 1:23–2:37 uden undertekst (2026-09-30)
Testet: 14 lokale modeller + Groq turbo, 5 vinduesplaceringer (offset 30/45/60/75/90 s). Fejl = ikke-mærket sangtekst tæller som tale i `gap_speech` og overstiger ordgrænsen (tiny 50, øvrige 100).
- Sikre (tale <4 s i alle 5): small.en, tiny.en-cpu, medium.en, base.en-q5_1-cpu, medium.en-q5_0, tiny.en-q5_1-cpu.
- Skriver sangtekst umærket (15–71 s "tale") men når ikke ordgrænsen 100: small.en-q5_1, base.en(-greedy), small.en-greedy, medium.en-greedy, alle turbo (lokal + Groq).
- Falder i fælden: tiny.en-greedy-cpu (1/5, 57 ord > 50). Appens egen tiny-kørsel (greedy) gav også falsk fund (20 s/51 ord).
- Konklusion: produktionsopsætningen (tiny greedy) er den ustabile; grænsen 50 ord for tiny er det svage led. Foreslået fix: tæl ikke umærket tale, der ligger sammenhængende med ♪/[MUSIC]-segmenter.
Data: docker_test/shameless_sang/

### Fix: sange tæller ikke som manglende midterparti (2026-09-30, ikke committet)
`gap_speech(..., music=)` + `music_spans()` (correctness.py): ♪ og musik-klammer ("(upbeat music)") i råtransskriptet; alt mellem første og sidste musikmærke i hullet (+8 s) ignoreres, kun tale udenfor sangen tæller. Forbundet i fuld-sti (`_missing_middle_hit`) og sampled probe. Shameless, 5 placeringer: 0/5 falske fund for alle modeller undtagen turbo (skriver aldrig ♪, 41–71 s "tale", men bar 100 ord). tiny.en-greedy: 1/5 -> 0/5. Enhedstests i tests/test_block_hole.py (SongWindowTests). IKKE endnu kørt: matrix for at bekræfte at indlagte huller stadig findes 100 %.

## Nye fejlafsnit: My Name Is Earl S02E03 + S01E07 (Docker verifyarr:new2, 2026-09-30)
- S02E03 (Known sync problem): rettet "fixed (Δ5.8s, 3 sync block(s))"; første cue 0.63→1.73 s; SUSPECT "partly_out_of_sync 20:20-20:59 (+2.5s)" – fejlen er delvist rettet, resten rapporteret. Skal verificeres manuelt.
- S01E07 (Missing sub from intro): undertekstens første linje "Good morning" står 3.2 s, i lyden 34.5 s (intro-fortælling 0–30 s mangler i filen => ægte offset ca. +31 s). Appen valgte 6 blokke, Δ42 s, flyttede "Good morning" til 0.0 s (forkert retning, overlappende cues) og flaggede SUSPECT. Rettelsen forværrede filen. Mangler: (1) tyst at intro mangler ikke fanges som missing_lines, fordi hullet først opstår efter en global forskydning; (2) alass-blokfit accepteres trods anker-residual 11–17 s (score 'ok'). Arbejdskopier: verifyarr-docker-test/earl/media.

### Fix: rest-stræk efter blok-fit rettes (2026-09-30, ikke committet)
`_repair_block_runs` + `_run_repair_plan` (pipeline.py): når fuld-transskript-detektoren (`_block_runs_hit`) finder et stræk linjer på et andet offset, flyttes strækkets cues med dets målte offset (kanter midt mellem nabo-cues, ud til filens slutning hvis strækket når enden). Skrives kun hvis den rettede fil bagefter ikke har noget stræk og dens median-residual ikke er værre; ellers uændret og detektoren flagger som før. Ikke i dry-run.
- My Name Is Earl S02E03: halen 20:20–20:59 (+2,5 s) rettet og verificeret (minut 20: +2,6 -> +0,3 s; alle minutter nu inden for ±1 s, ingen stræk). Verdict forbliver SUSPECT/partly_out_of_sync, fordi alass' 3-blok-fit altid giver "Block error repaired in 3 parts" (regel målt 26/9: 60 af 68 blok-rækker beholdt linjer 2 s+ ude) -> konklusionen er stadig "hent ny", men filen er bedre end før.
- FP-tjek: ingen stræk på de 5 Known Good med tiny.en-greedy (produktion); tiny.en (beam) giver på EUP ét stræk (-2,04 s ved 41:10, Whisper-drift på korrekt fil) som VAD-tjekket i `_block_runs_hit` skal afvise.
- Tests: tests/test_run_repair.py (3), 51 relevante tests grønne. Matrix med indlagte blokfejl ikke kørt.

### Test af blokreglen mod facit (SH, indlagte fejl, 2026-09-30)
docker_test/blokregel_test/: 6 SH-afsnit x 5 scenarier (kontrol, hale +2,5 s, hale +8 s, 3 blokke 3-25 s, drift 0->+5 s); alass-cli + stræk-rettelse; fejl målt mod den rigtige undertekst pr. cue.
- Sandt efter rettelse: 0-2,2 % af cues >1 s ude i alle 30 tilfælde; typisk median 0,0-0,2 s. Drift og 3 blokke rettes næsten perfekt (max 0,5-0,7 s ved drift; 3blokke 0,2-38 s på 0,3-2 % af cues).
- HUL: fejl i de sidste 40-60 s ses ikke af den tætte måling (stræk-detektoren fandt halen i 1 af 10 tilfælde; alass retter den heller ikke). 1-2 % af cues 2,5-8 s ude.
- Den tætte måling har støj: kontrolfiler (ingen fejl) når lokal median 1,27-1,35 s (SH2, SH6); tærskel på 1 s giver falsk alarm.
- Konklusion: blokreglen ("hent ny" ved >=2 alass-blokke) må ikke lempes på den tætte måling alene; halen er blind vinkel. S02E03's "under 1 s" var et skøn, ikke bevist.

### Hale-kontrol (siste 40-90 s): målt, ikke bygget (2026-09-30)
Forsøg på at lukke halens blinde vinkel (docker_test/blokregel_test/tail_explore.py, tail2.py, SH x6):
- Whisper-ankre: halen har 0-4 sammenlignelige punkter i SH (eftertekster uden dialog; SH3: 0, SH4/5: 1) -> for tyndt til stræk.
- VAD-mønster (tale vs stilhed, samme mål som shift_fits_speech) på de sidste 90 s cues: hale +8 s fanget i 3 af 6 (gevinst 0,29-0,36), ikke i SH3 (ingen tale), SH5 (støj), SH2 (forkert skift). Hale +2,5 s fanget i 0 af 6 pålideligt (gevinst 0,03-0,08, kontrolfil SH5 ligger på 0,05). 0 falske ved tærskel 0,2.
- Konklusion: halen kan ikke afgøres pålideligt med de nuværende beviser; ikke bygget ind, for en kontrol der fanger halvdelen af de store og ingen af de små giver falsk tryghed. Kun en håndtimet reference eller mere lyd-baseret evidens (ikke tynd dialog) kan lukke det.

## Frontend vs backend gennemgang + cloud Whisper fjernet fra checks (2026-09-30)
Gennemgang: alle 41 API-kald i frontend har en rute i backend; TypeScript-typer stemmer felt for felt med backend for 7 af 8 indstillingsgrupper og med DB-kolonnerne (files/runs/historik); alle 8 SuspectReason findes i verdict.ts. Fundet: (1) sync-gruppen har 7 backend-felter der ikke findes i UI/typer (anchor_suspect_min_samples, escalate_min_bad_samples [død, bruges ikke], escalate_only_multi_block, fps_require_full_coverage, vad_binary, vad_model, vad_min_speech_seconds) og 12 flere er kun i backend (clip_seconds, sample_count, window_minutes, overlap_threshold, line_order_*-tærskler, split_penalty, block_spread_suspect_threshold_s, m.fl.); (2) "no <provider> API key"-verdict pegede på cloud; (3) UI/README tilbød Groq/OpenRouter til checks.
Ændret: checks bruger kun lokal whisper.cpp. Fjernet correctness.stt_provider, use_local_whisper, groq_*/openrouter_* (STT+LLM+nøgler) fra Config, indstillinger, env-seed, UI (Settings-fanen Correctness, Wizard trin 3, typer) og README. Oversættelse af fremmedsprogede undertekster i checks bruger nu Generate-fanens LLM (Config.llm_call_kwargs). has_stt_configured = lokal binær findes; manglende binær -> "no local Whisper binary" (UI: "Can't check"). Generate (cloud STT + oversættelse) er uændret. Tests: tests/test_local_only_correctness.py (5) + 140 relevante tests grønne; tsc + vite build ok.

## Offline-vurdering af 10 Z:-afsnit uden video (2026-09-30)
Kilde: fulde Whisper-transskripter fra Docker-DB (docker_test/z10b_offline/*.transcript.json, 7 af 10; Lie to Me, S.W.A.T. S06E22, Shameless S06E03 klarede skærmtjekket på klip og har ingen fuld) + undertekster læst fra Z:. Script: offline.py. Ingen video overført.
- Community S03E20: konstant blok -25,7 s (ca. 2-12 min) og -19,4 s, resten i sync; ankerplanen (4 regioner) retter til 8/213 >2 s. Alass sagde "i sync" (Δ0,05 s) -> alass overser blokken; ankre fanger den.
- Taskmaster S06E02: konstant forskydning -4,08 s (kun 18/289 >2 s efter). Alass foreslog Δ109 s i 2 blokke -> alass' fit er vanvid, enkel konstant forskydning er rigtig.
- Brooklyn S01E02: lineær drift +1,27 s ved start, +10,2 s/time (+0,283 %); median rest 0,39 s efter linjefit. Alass Δ35,8 s passer ikke (ankre siger 2-5 s). Ikke-standard rate (ingen fps-forhold).
- S.W.A.T. S02E12: linjefit flad (-0,02 %), 22/299 >2 s (7 %, som sunde filer: BMS 10 %, HIMYM 8 %) -> sandsynligvis i sync; dry-run-dommen SUSPECT (alass 6 blokke, Δ209 s) ser ud som falsk alarm fra alass' forkerte fit.
- My Name Is Earl S03E13: kun 2 ankerpunkter (-38..-80 s) -> forkert undertekst, dommen står.
- HIMYM S04E03 og BMS S01E03: flade, ok.
Mønster: alass giver Δ35/109/209 s mens Whisper-ankrene viser <=5 s på tre filer. Kandidat til test: når alass afviger >~10 s fra en konsistent ankerfit, vælg ankerfittet (konstant/lineær/blokke).

## Video-uafhængighed (2026-09-30)
rapport/video_manifest.json: ffprobe for alle 70 videoer (længde, framerate 24000/1001 eller 50/1, lydspor, indlejrede undertekster). Afledte data pr. afsnit: wav (16 kHz mono, 69 afsnit, længde inden for 3 s af videoen), tiny.en-greedy-transskript (sweep_linux/tiny.en-greedy-cpu; nye slugs EARL_S02E03, BKLN_S01E03, SHAM_S05E06, EARL_S01E01), indlejrede undertekster udtrukket til embedded_subs/ (BMS eng; SH dan/fin/nor/swe; Stormester dan: 33 filer). test/Community S02E21 er byte-identisk dublet. Begrænsning: hele appen (Docker/Run single) læser selve videoen; kun offline-analyser og WAV-baserede tests virker uden den.

## Z5: alass alene vs vores app på 5 flaggede Z:-afsnit (2026-10-01)
Evaluator: small.en-greedy-transskript (uafhængigt af appens tiny), dense ankre mod hver undertekst; offset = lyd - cue (ideelt 0). docker_test/z5_eval.py/.json, Z5_STATUS.md. Filer: undertekst auto/Z5_flaggede/.
| afsnit | original | alass split7 | alass nosplit | vores app |
| COMM S03E20 | median -24,4 s, 62 % >2 s (blok -25,7/-19,4) | uændret (Δ0,05) | uændret | rettet: median 0,1 s, 6 % >2 s; rest 3 ankre op til 19 s; SUSPECT (4 regioner = blokregel) |
| BKLN S01E02 | +2,3 s, drift +0,28 % | -16,1 s: VÆRRE (90 % >2 s) | -15,8 s: VÆRRE | rettet (rate +0,28 %, +1,3 s): median -0,1, 8 % >2 s, ok |
| SWAT S02E12 | allerede i sync (median -0,2, 6 % >2 s) | 6 blokke op til 209 s: ØDELÆGGER (60 % >2 s) | -0,43 s: ok | valgte nosplit-fit (Δ0,4 s): ok, men filen var i sync (tørkørslens SUSPECT var falsk alarm) |
| TASK S06E02 | konstant -4,0 s | -114/-99 s: ØDELÆGGER | -88,5 s: ØDELÆGGER | FORKERT rate -4,04 % +85 s: første cue 6,9 -> 81,6 s, første 1-3 min op til 50 s forkerte; resten ok; derfor "missing_lines 82 s" = følge af egen fejl. Konstant -4,0 s ville give 6 % >2 s, min-median -0,9 |
| EARL S03E13 | forkert undertekst (kun 3 ankre) | - | - | wrong_subtitle: korrekt |
Hvad går galt: (1) alass giver Δ16-209 s hvor ankrene siger <=5 s (BKLN, SWAT split, TASK); (2) ramp rescue på TASK byggede en rate på et alass-2-bloks-trappetrin (tilt -78 s, rho -1,00) og anvendte den på en fil med ren konstant -4 s; (3) selektionen beholdt 'new' selvom noten viste old-residual 3,9 s mod new 14,7 s (TASK) og 3,0 vs 13,8 (BKLN) - skal undersøges i _resolve_ambiguous_sync (sammenlignes kun på fælles klip?). Idé: konstant/lineær ankerfit som egen kandidat; afvis kandidat hvis den ikke forbedrer tætte ankre mod originalen.

## Z5-rettelser og regression (2026-10-01, commit 7f0ff09, ikke pushet)
Rodårsag Taskmaster S06E02: originalen er flad -3,9 s; alass skrev -88 s med tidlige cues klampet til 0; den "rate" ramp rescue så var alass' eget trappetrin og blev anvendt (-4,04 %, første cue 7 -> 82 s). Alass' fps-gætning (--disable-fps-guessing) forklarer Brooklyn (-16 s mod +2,6 s) og dele af SWAT, men ikke Taskmaster/Community (alass er forkert uanset flag).
Rettelser: (1) `_baseline_shows_ramp`: rescue kun hvis originalens egen fulde pulje viser en rate. (2) Et vetoet alass-fit ender ikke længere i "hent ny" hvis originalens ankre giver en plan; anker-grenen retter (Taskmaster: Δ4,0 s, ok).
Regression: matrix tiny.en-greedy, fuld, audio off, 11 godkendte x 23 scenarier: 5/253 celler ændret. KG_BOB piecewise/_b/_c fra urettet (p50 8-10 s) til rettet (p50 0,08-0,18 s); KG_BOB uniform_neg fra forkert rate -4,09 % til rigtig konstant Δ45,0 s (p50 0,03 s); KG_BMS jitter anden årsagstekst. Ingen rate/PAL/uniform/drift-celle forværret. Docker: Taskmaster ok (Δ4,0 s), COMM/BKLN/SWAT/EARL uændret. Nye matrix-celler (REAL_CASE_SLUGS) + tests/test_real_cases.py (5), tests/test_ramp_guard.py (4). BEMÆRK: Community-videoerne er slettet, så matrixens default C_S03E03/E08/E10 springes nu over ("no video").

## Z100: de færdige build på 100 Z:-afsnit (2026-10-01/02) — løbende fund
Container verifyarr:final (commit 55fceb2), Z: monteret :ro, undertekst-KOPIER lokalt (originalen gemt som ORIGINAL.srt), ikke tør kørsel. Data: docker_test/z100/ (results.jsonl, analyze.py, Z100_STATUS.md).
Dommer = appens egne gemte fulde transskripter (video_full_transcript_cache), tæt anker-måling før/efter.

1. **KORREKT FIL OVERSKREVET (President Curtis S01E01):** original 0,2 s fra lyden (score 0,92 ok), alass' 4-bloksfit 0,15/0,35 SUSPECT, alligevel skrevet som Δ107 s (resultat 100 % forkert, flagget wrong_subtitle). Rod: `_reject_unproven_old` faldt tilbage til "new" når old var eneste kandidat der bestod indholdstjekket men manglede anker i en blok. Rettet (6a4e416): uden rival bliver old. Test: tests/test_old_wins.py (fejler uden rettelsen).
2. **Avatar S01E14, rate −3,90 % (op til 38 s) + SUSPECT missing_lines:** originalen har konstant ~+2,4 s med mild drift (+0,2 %); alass skrev sin egen −3,9 %-trappe (negative tider klampet til 0), ramp-redningen genbrugte DEN rate, fjernede alass' gæt og tabte de første 38 s af cues → falsk "mangler linjer". Rettet (d4bb216): en reddet rate skal være originalens egen (±0,5 %-point). Test i tests/test_ramp_guard.py.
3. **Chicago Fire S09E04 (#2, #36):** videoens lyd er et andet program (ingen seriefigurer, "Sidney MacBillion"); appen skrev alligevel underteksten om i 2 blokke (scores 0,19/0,36/0,22, flag SUSPECT). Åben: en blokfit med indholdsscore 0,36 'ok' blev beholdt over en original der også fejlede.
4. Observation: små rettelser (< 1 s) på filer der måler sunde: S.W.A.T. S02E17 Δ0,5 s (original median −0,1 s, efter +0,4 s), Californication Δ0,9 s. Inden for Whisper-støj; ikke endeligt vurderet.
5. Mulige tavse miss at undersøge: The Boys (ok, minut-median −11 s), Mr. Robot (−5 s), MINDHUNTER missing_lines.

### Z100, gennemgang af 85 færdige afsnit (2026-10-02, tæt dommer på appens egne gemte transskripter/klip)
- **Bob's Burgers S15E06 (#57): korrekt fil ødelagt.** Original 0,1→1,6 s over 20 min (let rate-drift), appen skrev en alass-fit på Δ146 s (nu −92…−141 s, 100 % >2 s). Loggen: old score 0,86 ok, anchor residual 0,8 s; new 0,14 SUSPECT, blocks 0,19 SUSPECT, alligevel "kept new". Samme klasse som President Curtis; containeren kører build 55fceb2 uden `6a4e416`. SKAL genkøres mod ny kode.
- **Brooklyn Nine-Nine S02E06 (#58): stille miss.** Originalen ligger +1,5…+2,3 s hele vejen (95 % >1 s), appen siger "already in sync" (alass Δ8,8 s afvist, old score 0,91 ok), SUSPECT partly_out_of_sync. Mål: forskydning skal rettes 100 %; anker-resync fyrede ikke. Undersøg.
- Chernobyl S01E02 (#3): fix korrekt (−58 s → ~0). Enkeltminut-udsving er 1–2 ankre = støj.
- Afsnit markeret OK/already in sync: ingen entydige fejl. Svage punkter: Band of Brothers S01E10 (5 minutter på −2,5…−4,6 s efter fix, 2–5 ankre/min), Mr. Robot S02E07 minut 38 (−2,1 s, 7 ankre, uændret). Øvrige udsving er enkelte ankre (støj).
- Uden data: Chicago Fire (#2/#36, lyd er et andet show), Top Gear, Family Guy, Futurama, samt springede (fremmedsprog/Klovn).

### Z100 afsluttet: 100 afsnit gennemgået (2026-10-02)
Dommer: appens egne gemte transskripter (52 fulde, 19 klip-baserede for resten), ingen egen udtrækning. 69 ok, 25 SUSPECT, 6 sprunget over (fremmedsprog/Klovn).
- **Korrekte filer ødelagt af dårligt alass-fit (3):** President Curtis S01E01, Bob's Burgers S15E06, Rick and Morty S04E05 (andel linjer >2,5 s forkert 2-7 % → 100 %). Mekanisme: original scorer ok (0,86-0,92), alass-fits SUSPECT, alligevel "kept new". Rettet i `6a4e416` (Rick and Morty reproduceret og bekræftet rettet i test) + ny sikring `_undo_rewrite_that_made_it_worse` (fortryder præcis disse tre, rører ingen af de 21 gode rettelser).
- **Rate/drift ikke rettet (gates for strenge):** Bob's Burgers (0,09 %, tilt 1,0 s), My Name Is Earl (+0,39 %, tilt 4,7 s, kun resid 0,52 > 0,40), The Bear (−0,35 %, tilt −6,1 s, resid 0,55). Rettet: snap-assisterede gates + stejl-ramp-gate (`0b8e763`, `51590aa`). Eureka (+0,65 %) rettes nu fra originalen i stedet for kun delvist.
- **Fast forskydning ikke rettet:** Brooklyn Nine-Nine S02E06 (+2,0 s hele vejen; alass Δ8,8 s afvist, intet andet forsøgt). Ny `_try_offset_from_dense` (indhold ok, pool dækker ≥60 % af filen, fladt+centreret efter).
- Delvist rettede, bør hentes ny: Band of Brothers S01E10 (20 % >2 s), Avatar S01E16 (14-16 %), The Bear (26 %), My Name Is Earl før rate-rettelsen (25 %).
- Korrekt detekteret (SUSPECT): Arrested Development x2, Gold Rush, Top Gear (forkert undertekst); MINDHUNTER, Maisel, The Boys, White Lotus, Better Call Saul, What We Do in the Shadows (missing_lines på sunde timings); Westworld (lokalt udsving ved minut 47).
- Uden data: Chicago Fire (lyden er et andet show), Family Guy, Futurama.
- Åbent: matrix-regression for de nye rettelser (må ikke give falske rettelser på Slow Horses); Z100 genkørt på ny kode for de 3 ødelagte + Brooklyn; fuld-vs-stikprøve-måling på de 47 stikprøve-afsnit.

### Slettede branches (2026-10-02)
- origin/generate-missing-subtitles var 95362a9 (0 commits foran main)
- origin/review-2026-09-28 var c5cd6c2 (0 commits foran main)
- lokal review-2026-09-28 var c5cd6c2. Lokal density (db06e14, 3 commits foran, ikke fusioneret) bevaret.

### De 4 flag, som kun fuld transskription fandt (stikprøve sagde ok) -- gennemgået mod lyd og transskript (2026-10-02)
- **FROM S04E03 missing_lines 6:42-9:18: FALSK.** Gabet er en sang ("Whatever will be, will be", 7:16-8:46) mellem tagsene [MUSIC] 7:06 og (upbeat music) 8:56. Music-vinduet samler kun mærker op til 60 s fra hinanden (`MUSIC_CLUSTER_GAP_S`); her er der 110 s, så teksterne midt imellem tæller som tale.
- **Westworld S01E01 missing_lines 66:44-68:06: FALSK.** Slutteksternes sang ("Ain't No Grave", 66:53-67:36). Sidste ♪-mærke er 66:58 + 8 s margin, men teksterne løber 38 s længere.
- **The Gentlemen S02E05 partly_out_of_sync: FALSK.** 3 enkeltankre ~-3 s i minut 49/55/57, hvor alle andre ankre i samme minutter ligger på -0,2 s; cuerne passer til talen (49:45: tiny.en slår to linjer sammen og placerer "He said no" 3 s for tidligt). Enkeltpunkter er støj; filen er rask (andel >2 s = 0,10 som sunde filer).
- **Chernobyl S01E02 missing_lines 41:15-45:30: SAND at der er tale uden undertekst.** Lyden er ikke engelsk tale (small.en-translate giver en evakueringsmeddelelse for Pripjat: "27th of April ... buses ... 14.00"), tiny.en skriver volapyk. Underteksten har ingen linjer i 4 min. Om den serie-version skal have undertekst til meddelelsen kan ikke afgøres fra lyden; tvivl -> hent ny er forsvarligt.
- Konklusion: 3 af 4 flag fra full er falske alarmer, 1 er sand/tvivlsom. Fuld transskription finder altså KUN 1 reel lokal fejl, som stikprøver ikke så, i de 24 afsnit.

### Test: bredere music-vindue mod falske missing_lines (2026-10-02) -- FORKASTET
Gentest på 84 afsnits gemte transskripter (resultatfilerne, tiny-bar 50 ord): (a) `MUSIC_CLUSTER_GAP_S` 60→150 s, (b) +45 s efter sidste mærke.
- (a) fjerner FROM (falsk) men også MINDHUNTER S?? 18:51-21:36 og What We Do in the Shadows 22:22-24:50, som begge har rigtig dialog i gabet (MINDHUNTER: samtale i en klub 19:16-21:36; WWDITS: "Dear Diary"-speak efter sangen, undertekst slutter 22:22 af 24:50). Mister to sandsynligt sande fund.
- (b) fjerner Westworld og S.W.A.T.-PSA, men skjuler MINDHUNTER/WWDITS' rigtige dialog endnu mere.
- Der er ingen regel i transskriptets mærker, der skiller sangtekst fra dialog. Mål er 100 % detektion af manglende midterparti, så falsk alarm koster en ny undertekst, men et misset fund koster en fejl. Ændringen er IKKE lavet; correctness.py uændret.

### Music-vindue gjort tilgivende (2026-10-02, efter brugerens afklaring)
MINDHUNTER S01E01 18:50-21:36 = indbrændte undertekster over musik; What We Do in the Shadows S03E02 efter 22:22 = reklamer for andre shows. Begge falske, så det brede vindue (150 s mellem mærker, +45 s efter sidste) er rigtigt: fjerner FROM, Westworld, MINDHUNTER, WWDITS og S.W.A.T.-PSA på de gemte transskripter. Tidligere "forkastet"-konklusion ovenfor er dermed omstødt. Commit på main (lokalt). Gap-detektion i matrixen (injicerede huller) skal stadig bekræftes.

### Stikprøver mod full på 36 afsnit (2026-10-02) -- resultat
Samme afsnit kørt i whisper_mode=full (image z47, HEAD 3f1c714) mod stikprøve-resultatet fra Z100; facit = tæt dommer på den fulde transskription.
- **Tidsfejl (forskydning, rate, drift): ingen misset af stikprøver.** 6 afsnit rettet i begge tilstande (Chernobyl, Californication, Umbrella Academy, How I Met Your Mother, Merlin, Resident Alien), slutresultat ens eller næsten (andel >2 s: 0,03-0,11 begge veje). Full lidt mere præcis på How I Met Your Mother (steady offset +1,3 s mod +2,1 s) og kalder Merlin som rate 23,976/25 i stedet for blok-offset.
- **Flag: 31 af 36 ens.** De 5 forskelle (kun full flagger): FROM, Westworld, The Gentlemen = falske; Chernobyl = tale uden undertekst, uklart; The White Lotus S01E06 = intet undertekst 0:00-2:20 (35 s tale, sandsynligvis åbningssang/recap; ikke afklaret).
- Konklusion: fuld transskription gav ingen ekstra reelle fund her og kostede ca. 1,3x tid. Stikprøver + eskalering ved uenighed er nok til tidsfejl; full tilføjer kun lokal detektion (missing_lines), hvor 4 af 5 var falske.

### Matrix-regression, 11 godkendte afsnit (6 SH + 5 KG), tiny.en-greedy-cpu, audio-confirm off, HEAD 1409b13 (2026-10-02)
Mod `matrixdata/godkendte_2026-09-30/matrix_alle_modeller.jsonl` (samme model, 506 rækker, alle status ok).
- clean: 0 SUSPECT og 0 omskrevne, le1s 1,000 i full og sampled. drift, drift_offset, drift_swap, fps_early/late, pal_early/late, uniform*, dropdup: uændret 1,000. missing_middle 11/11 SUSPECT i begge tilstande (ingen tabt detektion med 2-min-kanterne). wrong_episode 11/11.
- Forbedret: uniform_neg falsk missing_lines 8 → 0 (kanterne). cut_version le1s 0,805→0,844 (full) / 0,764→0,844 (sampled); piecewise 0,747→0,917 / 0,708→0,754; piecewise_b 0,859→0,941 / 0,771→0,924; piecewise_c full 0,695→0,751.
- Forværret: piecewise_c sampled 0,770→0,727; jitter 0,321→0,083 / 0,236→0,083: bevidst (fortryd-sikringen lægger den jitterede fil tilbage urørt; scenariets bestå-krav er "ikke værre end indlagt"), SUSPECT bevaret 11/11.
- Første kørsel (før `1409b13`) mistede detektionen på 9 rækker (jitter/cut_version SUSPECT→ok): sikringen ændrede verdictet. Rettet: den lægger kun filen tilbage.
- Testsuite på 91def0c: 462 bestået, 17 sprunget over, 0 fejlet.

### Musik-detektion, 5 sange i Community S03E10 (menneske-mærkede ♪-cues, 30 s kontekst før/efter), 6 modeller x 3 indstillinger
Rådata i `whisper_gpu_staging/musiktest/` (songbench.py, songbench_results.json, raw/).

## Musik-detektion: ændrer præcise musikgrænser noget på Z100? (2026-10-02)

Test: 26 hul-kandidater i de 94 Z100-afsnit med fuldt transskript (huller hvor tiny-transskriptet har >=15 s / >=50 ord tale og undertekst mangler). For hver kandidat kørte small.en + `--prompt` + `--carry-initial-prompt` på hullet +-30 s; small's musikmærker blev brugt som præcise vinduer (+-5 s, sammenføjet <=20 s) i stedet for det nuværende brede vindue (tiny-mærker, 150 s klynge, 45 s hale). Filer: musiktest/z100_gaps/ (compare.out).

Resultat: 17 huller flagges i dag, 16 med præcise vinduer. 7 af 26 skifter dom:
- 3 NYE flag: MINDHUNTER 18:55 (kendt falsk: tale over musik med burned-in undertekst, flaget kommer tilbage), Family Guy 15:55 og Rick and Morty 7:06 (rigtig dialog uden undertekst, skjult i dag af ét enkelt falsk tiny-musikmærke).
- 4 FJERNEDE flag: The Bear 28:18, Gold Rush 22:20, Family Guy 16:30, Top Gear 52:10. Alle fire er dialog mellem/over sange (small's tekst viser køkkensnak og replikker mellem [MUSIC]-mærker), ikke sang. Præcise vinduer ville skjule rigtige huller.
- Ingen af de kendte falske alarmer bliver løst; MINDHUNTER bliver værre.

Konklusion: bedre musikdetektion fjerner ingen kendt falsk alarm og skjuler 4 sandsynligt rigtige huller. Byg det ikke. Eneste lærdom: ét enkelt falsk tiny-mærke kan i dag skjule et helt hul (Family Guy, Rick and Morty); kan overvejes at kræve >=2 mærker før et vindue åbnes. Forbehold: 26 huller, ingen facit ud over tekstlæsning, og Rick and Morty/Family Guy-filerne er output fra den tidlige Z100-kode.

## NYT FACIT: Known Good (2026-10-03)
Brugerens ordre: alt facit, alle konklusioner og alle dokumenter baseres kun paa de 10 verificerede afsnit i `Known Good\` (BB, BIL, BMS, BOB, EUP, PB, SHAM, BOYS, SH_S01E01, SH_S01E06). Tidligere saet (Community, Z5, Z100, de 11 godkendte) er arkiveret og bruges ikke i noget citeret tal.
Datasaet i repoet: `tests/known_good/` (undertekster, 15 modeller x 10 afsnit transskripter, VAD, optagede alass-svar, resultater; ingen lyd/video). Matrix: 10 afsnit x 58 scenarier x 15 modeller x full+sampled, audio-confirm off = 17.400 raekker, 0 pipelinefejl, koert som ren replay uden lyd. Windows-GPU-sessionen lavede 3 afsnit x 14 lokale modeller (Vulkan/CPU, log i kg_sweep_gpu/); Groq blev kort via appens egen transskriber (noeglen ligger i appens settings-DB, ikke i miljoeet); PB x medium.en-greedy blev kort paa CPU her (rc=127 i Windows-sessionen).

Fund:
- Detektion/rettelse adskiller ikke modellerne: offset 49-50/50 for alle, rate 149-159/160, wrong_episode 10/10, swap 20/20.
- **Sunde filer** adskiller dem: tiny.en-greedy 10/10 i begge modes. Full mode: small.en 5/10, medium.en 6/10, turbo-q5_0 6/10, base.en 7/10. 48 af 52 falske alarmer er partly_out_of_sync fra blok-detektoren (kalibreret paa tiny-greedy), 4 er missing_lines.
- **Sampled** misser 6/200 blokfejl (194/200 vs 199/200 i full) for default-modellen.
- **Huller**: opdages naar ca. 20 dialoglinjer fjernes (20-39 linjer 82 %, 40+ 95 %); 0-9 linjer 18 %, 10-19 linjer 36 % (alle modeller, begge modes). Afskaaret start/slut: <150 s 24 %, 150-250 s 58 %, >=250 s 81 % (de foerste/sidste 2 min doemmes bevidst ikke).
- **Ikke-determinisme**: uden fast PYTHONHASHSEED afviger noter/klipantal i mange raekker, og dommen (flag/reason) i 18 af 17.400 raekker (0,10 %, kun sampled; jitter 9, cut_version 5). Aarsag ikke fundet (ingen aabenbar set-iteration). Med fast frø er to koersler identiske (0/116). Matrixen saetter selv frøet. Boer undersoeges: appen koerer med tilfaeldigt frø.
- **VAD bruges ikke til klipplacering paa videofiler** (`run_vad_timeline` returnerer None for alt der ikke er .wav). VAD-filerne ligger i datasaettet men paavirker ikke matrixen.
- Byttede linjer er ikke verificeret af brugeren; heuristikken flagger 22 linjer paa de 10 urørte filer (1,4,0,4,1,1,1,5,1,4).
- alass-svar skal optages (`VERIFYARR_KG=auto`) for at koere nye rettelser: en kode-aendring der giver alass et andet input faar "alass replay miss" i replay. Optagelsen fylder 8,7 MB (xz; gzip gav 82 MB).
- Musik-prompt-testen blev genbrugt uaendret (anden materiale: Community + Slow Horses-kontroller); Z100-gap-testen om musikgraenser indgaar ikke laengere i det citerede facit.

### /code-review high af Known Good-arbejdet (2026-10-03)
Rettet: (1) smoke-testen rydder kun sin egen shard-mappe; (2) `_undo_rewrite_that_made_it_worse` kører nu også før de to tidlige returns (swap-port og vetoed bad fit); (3) model_quality: nævneren i "clips >= 3 anchors" er alle 30 s-vinduer (tal faldt fra ca. 0,69 til ca. 0,50-0,58); (4) merge_alass sletter ikke en part-fil, den ikke kunne laese helt; (5) hul-bins tæller ikke raekker uden removed_dialogue; (9) os.execv til hash-seed kun paa POSIX + seed saettes i shard-miljoeet; (10) dødt END_IGNORE_S og redundant max() fjernet.
Ikke aendret, med data fra matrixen (17.400 raekker):
- snap-gate/rate-gates (anmelderens pkt. 6): 0 rate-/offset-omskrivninger paa clean, dropdup, jitter, wrong_episode og swap. MEN 20 raekker blev rate-fixet paa en blokfejl: 16 x KG_BOB cutsteps_rand5 (en trappe af klip, -4,11 % "stretch", fundet af de normale gates: 96 % paa linjen, rho -0,99). Fejlen blev ca. dobbelt saa stor (injiceret p50 53,6 s -> 110,2 s efter) og sync-status siger "fixed", men filen er flagget SUSPECT wrong_subtitle. Sikkerhedsnettet saa det ikke, fordi det kun sammenligner paa det stykke originalen matchede. 2 x piecewise_c SHAM (7,7 s -> 1,25 s, forbedret) og 2 x missing_middle BOB (0 -> 0,15 s).
- offset-fix tilt < 1,0 s (pkt. 7): alle 264 omskrivninger er uniform-scenarier; Brooklyn-tilfaeldet (tilt ca. 0,8 s) kraever graensen, saa den strammes ikke.
- stjerne-regex (pkt. 8): 0 stjerne-celler i de 10 verificerede undertekster, saa ingen effekt maalt; risikoen (rigtig tale i *...*) er hypotetisk.
Aabent beslutning: skal en fil der ender SUSPECT efter en rate-omskrivning have originalen tilbage (konservativt), eller beholdes omskrivningen (nu: beholdes, fordi blokrettelser ofte forbedrer)? Kraever ny matrix.

## Fuld transskription vs udklip, tiny (2026-10-03, Known Good-matrix)
- 1.740 par (3 tiny-opsætninger x 10 afsnit x 58 scenarier): kun fuld bestod 35, kun udklip 17, nettogevinst ca. 1 %.
- Næsten alt i blokke: fuld flagger 596/600, udklip 576/600. De 20 misser var ikke eskaleret (udklippene var enige).
- Alt andet ens; rettede blokke ens (ca. 23 %). Udklip eskalerer alligevel til hel transskription i 94 % af blokkene.
- Tiny fuld ca. 2,3 min/afsnit; større modeller 4-10x dyrere, så de beholder udklip.
- Beslutning: `sync.whisper_mode` har nu `auto` (standard) = full ved tiny, sampled ved andre. `sampled`/`full` kan tvinges i Settings.
- Ikke ændret: rate-omskrivning der ender SUSPECT (cutsteps_rand5) afventer brugerens valg.
- Rate-omskrivning på wrong_subtitle-fil fortrydes nu (17 rækker, KG_BOB cutsteps_rand5). Første forsøg (fortryd alle SUSPECT-rate) ramte 453 korrekte rate-rettelser og blev droppet; poolmålet kan ikke se fejlen (de ikke-matchende linjer udelades), så årsagen wrong_subtitle bruges. Matrix kørt igen, ingen andre regressioner.

## Z100-genkørsel på nyeste kode + brugerens verifikation (2026-10-04)
Brugerens dom (efter at have set filerne): Chicago Fire S09 = fransk tale, sæsonen slettet. Arrested Development S04E02 rigtigt flagget forkert. Top Gear S09E04 rettet korrekt til 28:09, derefter forkert -> korrekt flagget, hent ny. Chernobyl S01E02 forkert undertekst (nu slettet fra Z:). Westworld S03E07, What We Do in the Shadows S03E02, Better Call Saul S04E07: originalen er korrekt. Blue Mountain State S02E08, Merlin S04E11, Robot Chicken S02E18: korrekt rettet.
Fundet og rettet:
- Avatar S01E14: de første 12 cues blev stablet på 38,1 s (alass klipper cues <0 til 0, rate-rettelsen oven på sendte dem alle samme sted). `_unclamp_alass_output` lægger dem tilbage; ny kørsel: max 1 cue pr. starttid, midten uændret rigtig.
- South Park S16E04: 0,2 % drift ikke rettet, gain 0,197 mod bar 0,20. Bar sænket til 0,15 (ingen nye afsnit på 87 Z100-afsnit passerer). Ny kørsel: rate +0,18 %, alle 5-min-bins inden for +-0,1 s (før -1,3 s ved 20 min).
- Westworld S03E07: veto-grenen flagede en ren original. Nu ikke flaget fra vetoen; MEN nu viser et andet tjek sin rigtige årsag: past_audio_end (85 cues starter efter videoens slut, video 58:24, undertekst slutter 63:27, dialog "What comes after the end of the world?"). Skal afgøres af brugeren: afkortet video eller længere undertekst.
- The Bear S01E06 (28:17-28:38, køkkenbaggrundssnak) og The Boys S05E07 (64:03-68:10, sangen "Dream a little dream of me" over slutningen): falske missing_lines. Ikke rettet; tail-gab/baggrundssnak kunne nedgraderes.
- Better Call Saul og fremmedsprogs-undertekster: forslag = sprogdetektion på flaggede gab; ikke bygget.
Tests: 503 bestået, 0 sprunget over i ren klon (Known Good-data i repoet). Tests der krævede Community/SH02-05/Z5-data er porteret til Known Good eller fjernet.
