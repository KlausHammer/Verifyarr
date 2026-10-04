# Review af detektion/rettelse i verifyarr (HEAD d9a7ad2)

Read-only review. Ingen filer i repoet er ændret. Alle kørsler lå i min scratchpad med egne,
friske DB'er (`/tmp/claude-1000/-home-hammer/037bfcdf-32e4-4d77-b54d-7c7879c6ad97/scratchpad/probe/`).

## 0. Metode: hvad der er verificeret, og hvad der er mistanke

- **Verificeret ved kørsel (probe).** Jeg kørte `tests/e2e_matrix.run_one` (den rigtige kæde:
  screen -> sync_pair/alass -> correctness_and_finish), en frisk DB pr. række, med modellen
  **tiny.en-greedy-cpu** (den valgte). Scenarierne er realistiske varianter, som matricen aldrig
  indlægger: huller på 90-200 s, afkortet start/slut, én blok på 180 s (±3-6 s) midt i, i starten
  og i slutningen, 6 små blokke (±2-4 s), offset 0,35/0,4/-0,6 s, drift 0,3/0,5/1 %,
  25->23,976 fps begge veje, PAL + manglende del, jitter ±0,5-1,5 s og ±2-5 s, samt clean-kontrol.
  Kørt på alle 6 Slow Horses-afsnit (SH) og på C_S03E03/E08/E10, begge modes. I alt 390 rækker.
  Data: `probe.jsonl`, `probe2.jsonl` (Community), `probe3.jsonl`. Script: `probe.py`, opsummering: `summ.py`.
- **Verificeret ved måling uden pipeline.** Detektionskurve for manglende midterparti: huller på
  60-300 s lagt på 18 positioner (5-90 %) i 9 afsnit, dømt med den rigtige
  `missing_middle_evidence` (`mm_curve.py`). Raske cue-huller på tværs af alle 15 modeller
  (`mm_music.py`, `mm_healthy.py`).
- **Verificeret i eksisterende data.** `tests/sh_all15.jsonl` (15 modeller) og
  `tests/sh_jit2_genuine.jsonl` (ægte afsnit) genlæst og talt op.
- **Læst, ikke kørt.** Alt markeret "(læst)" nedenfor er udledt af koden alene.

Clean-kontrollen var ren: 18/18 clean-rækker blev "ok" og urørte. Alle fund nedenfor gælder
altså varianter af fejl, ikke den raske fil.

### Samlet billede fra proben (tiny.en-greedy, SH = 12 rækker/scenarie, Community = 6)

"Rettet" betyder her p50 ≤ 0,15 s og ≥ 98 % af cues inden for 0,5 s. "Tavs" betyder flag ok uden
den betingelse, altså hverken rettet eller advaret.

| scenarie | SH | Community | skal |
|---|---|---|---|
| hul 90 s midt i | 0/12 detekteret | – | detekteres |
| hul 120 s | 2/12 | 4/6 | detekteres |
| hul 150 s | 8/12 | 6/6 | detekteres |
| hul 150 s nær start | 8/12 | – | detekteres |
| afkortet slut (sidste 200 s) | **0/12** | **0/6** | detekteres |
| afkortet start (første 150 s) | **0/12** | 0/6 (1 omskrevet og ødelagt) | detekteres |
| én blok 180 s, +3 s | 4 rettet, 1 advaret, **7 tavse** | 0 rettet præcist, 6 tavse | detekteres |
| én blok 180 s, +6 s | 2 rettet, **10 tavse** | 1 rettet, 4 advaret, 1 tavs | detekteres |
| blok i starten (0-180 s, -4 s) | 5 rettet, **7 tavse** | – | detekteres |
| 6 små blokke ±2-4 s | 8 rettet, 4 delvist og tavse | – | detekteres |
| offset +0,35 / +0,4 s | 22/24 rettet, 2 urørte | 2/6 rettet | rettes |
| drift 0,3 % | 3/12 rettet, **9 tavse** | **0/6, 6 tavse** | rettes |
| drift 0,5 % | 12/12 | 0 rettet, 4 advaret, 2 tavse | rettes |
| 25->23,976 fps | 20/24 præcist, 4 med 0,25-0,28 s tilbage | – | rettes |
| jitter ±0,5-1,5 s | 8 advaret, 4 tavse, **alle 12 omskrevet** | 2 advaret, 4 tavse, alle omskrevet | detekteres, ikke rettes |
| jitter ±2-5 s | 12 advaret, **alle omskrevet til det værre** (p50 op til 31 s) | samme | detekteres, ikke rettes |

---

## 1. Fund, mest alvorlige først

### H1. Screen-"ok" springer alass over, og ok-dommen er blind for mindretal (høj, hul/overfitting)

`pipeline.py:274-277` kalder filen "ok", når median-offset, MAD-spredning og tilt alle er under
0,25 s. `sync_pair` (`pipeline.py:572-580`) stopper derefter uden alass. Median og MAD er bygget
til at ignorere op til knap halvdelen af klippene. En blokfejl ER et mindretal af klippene.
Kommentaren ved `SCREEN_MIN_AGREE_FRAC` (`pipeline.py:184-185`) fravælger bevidst
enighedskravet på ok-dommen.

Bagefter er næsten al eskalering slået fra for sådan en fil. `_screen_says_needs_full`
(`pipeline.py:1581`) kræver en flerblok-fit fra alass (`escalate_only_multi_block` er True som
standard), men alass kørte aldrig. Tilbage er kun `significant_anchor_residuals` med 2 klip
(sampled) eller 3 (tæt evidens).

**Verificeret:** SH_S01E01, en blok på 180 s forskudt 6 s, **full mode**: screen læste
`agree_frac 0.55`, men dommen blev "ok". Alass blev ikke kørt. De to ankre inde i blokken
(-6,96 s og -4,51 s, ved 1277 s og 1377 s) er "støj" under 3-ankerreglen, og filen ender som ok
og urørt (`dbg_block.py`). I proben er 17 af 24 SH-rækker med en 180 s-blok tavse. Det samme
gælder 7 af 12 med en blok i starten og 4 af 12 med en blok i slutningen. Ægte data:
C_S03E01 (blok 0:21-3:39, 19 s for sent, brugerbekræftet) screenes "ok", og alass køres ikke.
Den fanges kun, fordi 19 s giver mange ankre.

**Forslag:** Ok-dommen skal også kræve, at ingen klynge af ankre afviger. Konkret: ingen to
konfidente ankre inden for fx 5 minutter afviger over 2,5 s fra medianen, og intet enkelt anker
afviger over 5 s. Ellers er dommen "needs_sync", så alass kører. Det koster kun alass-tid (billigt
i forhold til Whisper) og er ingen dom over filen. Alass afgør bagefter om noget flyttes. Det
følger brugerens rollefordeling: Whisper finder stedet, alass retter.

### H2. Screen-tolerancen på 0,25 s ligger inden i Whispers egen bias, så 0,3-0,45 s offsets slipper igennem (høj, overfitting)

`SCREEN_TOLERANCE_S = 0.25` (`pipeline.py:176`) er sat lig med `min_change_seconds`. Men
Whisper-segmenter sidder systematisk forskudt fra cue-start. **Målt** i `sh_all15.jsonl` på
clean-rækker ligger screen-offset pr. model mellem -0,21 og +0,17 s (turbo-familien er
systematisk -0,05..-0,20, base-familien +0,03..+0,17). En ægte +0,4 s kan derfor aflæses som
0,2 s og vinkes igennem uden alass.

**Verificeret på ægte data:** C_S02E12 er ifølge brugeren "forskudt ca. 0,4 s". I sampled mode
screenes den til +0,20 s, dommen er "already in sync", og alass køres ikke. Filen rettes
**ikke** (`sh_jit2_genuine.jsonl`). Full mode retter den. Proben viser samme mønster med +0,35 s:
5 af 18 rækker blev ikke rettet.

**Forslag:** Screenen må ikke afgøre "ok" med en tolerance, der er mindre end modellens bias
plus støj. Enten køres alass altid (billigt), og screenen bruges kun til at vælge
presync/eskalering, eller ok-båndet strammes til fx |median| < 0,1 s, så alass kører oftere. Man
bør ikke kalibrere en bias pr. model, da det kun hjælper enkelte modeller.

### H3. Rettelser skrives ud fra Whisper-medianer uden præcis efterjustering (høj, strider mod designprincippet)

Tre steder skriver en Whisper-målt størrelse direkte i filen, uden at alass eller VAD finpudser
den bagefter:

- `_try_anchor_resync` (`pipeline.py:1146-1203`): hver regions skift er medianen af
  (segmentstart - cuestart). Regioner under `ANCHOR_RESYNC_MIN_SHIFT_S` flyttes **også**, så
  snart én region er over 1 s (`subtitles.py:929` tjekker kun "alle under").
  **Verificeret:** en blok på 180 s i Community C_S03E03/E10 giver planen
  `[+0.2s x21, -2.7s x9, +0.2s x22]`. De korrekte 90 % af filen flyttes 0,2 s, og p50 efter
  rettelsen er 0,21 s. Turbo SH_S01E01 clean (`sh_all15`) giver `[-0.1 x54, -3.7 x5, -0.4 x27]`:
  en korrekt fil får 5 ankres -3,7 s skrevet ind plus -0,4 s i sidste tredjedel.
- Presync-raten (`presync_from_screen`, `pipeline.py:305-310`) og stretch-rettelsen: raten er
  Whisper-fittet. **Verificeret** i `sh_all15`: PAL med presync-rate har 76 af 462 rækker under
  95 % inden for 0,5 s (min 0,63). Samme scenarie uden presync, hvor alass alene gættede PAL,
  har 0 af 250 under 95 %. Rate-fejlen er typisk 0,03-0,1 procentpoint (op til 0,32 i sampled),
  hvilket giver op til 1 s i slutningen, og alass `--no-split` kan ikke fjerne en rest-rate.
  I proben: 25->23,976 fps 4/24 med p50 0,25-0,28 s tilbage.
- Den diskrete fps-rettelse vælger ratio alene ud fra tiltens fortegn (`pipeline.py:1461`) og
  tjekker aldrig, at den målte hældning faktisk er ≈ 0,1 %. **Verificeret:** SH_S01E03 med 0,3 %
  drift fik "24 -> 23.976" (anker-tilt -4,54 s, VAD -0,48 s). Quartil-porten på 1,5 s lod den
  passere, 0,2 % blev tilbage (p50 0,70 s), og flaget er ok.

**Forslag (gælder alle modeller):** (a) Snap en målt rate til nærmeste standardforhold (25/24,
24/25, 25/23,976, 23,976/25, 1001/1000, 1000/1001), når den ligger inden for fx 0,15
procentpoint. Ellers brug den målte rate. (b) Den diskrete vej kræver
|målt hældning - (ratio - 1)| < fx 0,03 %. (c) Efter hver Whisper-drevet omskrivning (resync,
stretch, presync) skal en præcis aligner køre på resultatet. Alass på hele filen løftede i 14.27
alle halvgode rettelser til ≥ 0,92 og gjorde intet værre. Alternativt VAD-onset-match pr. region
(vadalign.py, 14.28, ±0,15 s). (d) Regioner under 1 s får skift 0.

### H4. `_resolve_ambiguous_sync` skriver alass' fit, også når ankrene viser at originalen er rigtig (høj, bug i logikken)

`_confirmed_in_every_block` returnerer False, når `blocks_time_ranges` er tom
(`pipeline.py:917`). Ved en single-block-deferral er den altid tom (`pipeline.py:713`). Så længe
der findes ankre, er `_old_wins_fairly()` (`pipeline.py:947`) derfor **altid** False, og "old"
kan aldrig vinde en single-block-sammenligning. Ved flerblok-fits kræves det, at "old" har et
anker inden for 1 s i **hver** af alass' blokke, også i blokke alass har opfundet over områder
uden cues.

**Verificeret:**
- C_S03E03 jitter ±0,5-1,5 s: alass flyttede filen 8,4 s. `new` har residual 8,5 s over 14 klip,
  `old` 0,7 s over 13. Alligevel skrives `new`, og anker-resync flytter så -8,4 s tilbage. Flaget
  bliver "ok", og jitteren rapporteres aldrig, fordi post-resync-grenen springer jitter over (M2).
- C_S03E08 afkortet start (150 s): `old` 0,2 s over 15 klip, `blocks` 12,3 s. `blocks` skrives
  (p50 9,0 s), og filen ender SUSPECT, men ødelagt på disken.
- C_S03E08 med en blok på 180 s: `new` = alass' globale 0,4 s (trukket af blokken), uafgjort mod
  `old`. `new` vinder på præference. De korrekte 90 % får 0,4 s fejl, og blokken står urettet og tavs.
- Jitter ±2-5 s: alle 18 rækker omskrevet til det værre (SH_S01E05 p50 3,3 -> 30,9 s), dog flagget.

**Forslag:** Når en kandidats egne ankre er signifikant dårlige (≥ 3 klip > 2,5 s), og "old" er
tydeligt bedre på de fælles klip, må kandidaten **ikke skrives**. Behold "old", og flag. Single-block
behandles som én blok [0, ∞). "Old" skal højst bevise sig i blokke, hvor den har cues. Og når den
endelige dom er SUSPECT, fordi ankrene modsiger det skrevne, bør filen rulles tilbage til
originalen (backup findes), så en advaret fil ikke også er en forværret fil.

### H5. Manglende midterparti: tærsklerne passer til det indlagte 300 s-hul, ikke til virkelige huller (høj, overfitting)

`MISSING_MIDDLE_MIN_SPEECH_S = 90`, `MIN_WORDS = 200` (`correctness.py:732-737`) er absolutte og
uafhængige af hullets længde og af taletætheden. **Målt** med den rigtige
`missing_middle_evidence` på 18 positioner × 9 afsnit (tiny.en-greedy, full):

| hul | 60 s | 90 s | 120 s | 150 s | 180 s | 240 s | 300 s |
|---|---|---|---|---|---|---|---|
| detekteret | 0 % | 1 % | 47 % | 65 % | 78 % | 91 % | 97 % |

Selv 300 s fanges ikke overalt (SH_S01E01 15/17). Matricens 24/24 skyldes, at hullet altid
ligger ved 40 %. Sampled mode køber først fuldt transskript ved cue-huller ≥ 120 s
(`MISSING_MIDDLE_ESCALATE_GAP_S`), så et hul på 90-119 s bliver aldrig set dér.

Hvorfor barren skal være så høj: de raske huller med flest ord (133 ord) er **seriens
titelsang** med ♪-mærker ("Surrounded by losers, misfits and boozers ♪"). Den er ikke dialog.
**Målt:** fjernes ♪-segmenter, falder den raske maksimum på SH til 34-72 ord for 13 af 15
modeller. turbo-q5_0/q8_0 mærker aldrig ♪ og ligger på 86/75 ord. Community har 0-7 ord. Med
♪-filter og barren "≥ 120 ord og ≥ 50 s tale" stiger detektionen til: 90 s 68-78 %, 120 s
84-88 %, 150 s 90-94 %, 180 s 95-97 %, 240-300 s ~99 %. Det er målt på tiny.en-greedy og
turbo-q5_0, der ikke mærker ♪, så forslaget er ikke afhængigt af én models tags.

**Forslag:** (a) Tæl ikke ♪/♫-segmenter som tale. (b) Sænk barren til ca. 120 ord / 50 s. Det
skal verificeres mod raske filer med sange uden undertekster og med mindst én serie mere end SH.
(c) Sampled: køb ikke hele transskriptet for et hul. Transskribér hullet selv (plus lidt margin):
det koster hullets længde i stedet for hele afsnittet. Så kan eskaleringsgrænsen sænkes til fx
45 s uden at N100'en betaler for det. (d) Brug en anden undertekst til samme video som ekstra
evidens, hvor en findes (dansk og engelsk ligger side om side). Har den cues i hullet, er det
ikke en scene uden dialog.

### H6. Afkortet undertekst (manglende start eller slut) detekteres slet ikke (høj, hul)

`cue_gaps` (`correctness.py:740-744`) ser kun huller **mellem** cues. Hovedet før første cue og
halen efter sidste cue kigges der aldrig på. `SCREEN_MAX_TAIL_GAP_S` (`pipeline.py:190`) sender
kun filen videre til "den normale kæde", og der findes ingen hale-dom i den kæde.
**Verificeret:** de sidste 200 s fjernet giver 0/18 detekteret. De første 150 s fjernet giver
0/18 (i én række blev filen også ødelagt, se H4). En afkortet download er en af de mest
almindelige virkelige "manglende del"-fejl.

**Forslag:** Behandl [0, første cue] og [sidste cue, varighed] som huller i
`missing_middle_evidence` med samme ord- og tale-barre. Rulletekster og ♪ filtreres som i H5.

### H7. Drift mellem 0,1 % og ca. 8 s tilt er en død zone (høj, overfitting)

`STRETCH_MIN_TILT_S = 8.0` (`subtitles.py:303`, "below this the discrete ratios own the case") er
i sekunder, men de diskrete forhold dækker kun præcis ±0,1 %. Hvilke rater der kan rettes, afhænger
derfor af afsnittets længde: ca. 0,63 % på 21 min og 0,25 % på 53 min. `FPS_MAX_BASE_SPREAD_S`
(1,0 s) og quartil-porten afviser mellemrater på den diskrete vej, og alass laver en trappe, som
anker-tærsklen på 2,5 s ikke ser.
**Verificeret:** drift 0,3 % på Community gav 6/6 tavse (p50 0,67-1,04 s efter). På SH 9/12
tavse. Drift 0,5 % på Community: 0 rettet, 4 advaret, 2 tavse. Matricen tester kun 2 % og
1001/1000, der ligger på hver sin side af zonen.

**Forslag:** Stretch-vejen skal gates på rate-signifikans og ikke på absolut tilt. Fx
|tilt| ≥ max(2 s, 3 × anker-støjens MAD) sammen med de eksisterende keep_frac, rho og resid, og
snap til standardforhold (H3a). Raske filer har tilt ≤ 0,72 s ifølge `subtitles.py:241-242`, så
2-3 s giver stadig margin. Det skal måles på clean, gap og piecewise før det tages i brug.

### H8. Der er ingen endelig restkontrol af den skrevne fil, så fejl på 0,5-2,5 s er usynlige (høj, hul)

Hver detektor dækker sin egen form med barrer på 2,5 s og derover (`ANCHOR_SUSPECT_THRESHOLD_S`).
`anchor_run_offsets` (1,2 s, k = 10) kører kun efter en resync. Så en fil, som alass eller
fps-rettelsen har efterladt 1-2 s ved siden af, bliver "ok". **Verificeret:** SH_S01E04 drift
0,3 % full: `new` (Δ5,1 s) blev valgt over `blocks` (residual 1,6 mod 0,7, margin 1,0 ikke
nået), og resultatet er p50 1,25 s, flag ok. Tilsvarende for den forkerte fps-ratio (H3).

**Forslag:** Kør én sidste kontrol på den fil, der faktisk ligger på disken, uanset hvilken gren
der skrev den: kvartil- eller 5-minutters-residualer på den fulde anker-pulje, når den findes, og
ellers på VAD-onsets. Det er kun detektion (SUSPECT), aldrig en rettelse. Barren sættes ud fra
raske filers maksimum pr. model (run_offsets-data siger 0,86 s ved k = 10), så en realistisk bar
er ca. 1,0-1,2 s. Det er Whisper-lokatorens opløsning. Præcision under det kræver ægte VAD (M3).

---

### M1. Få ankre på 3-5 s kan omskrive en korrekt fil (middel, strider mod "grov lokator")

Regler, der handler på en håndfuld ankre:
- `significant_anchor_residuals` med min 2 (sampled, `settings.py:637`) eller 3 (tæt,
  `pipeline.py:1875-1878`) åbner anker-grenen. Den forsøger `_try_anchor_resync`, som **skriver**.
- `ANCHOR_REGION_MIN_ANCHORS = 3` (`subtitles.py:802`): en region på 3 ankre (60 s ved 20 s
  interval) får sit skift skrevet.
- `clearly_better` (`pipeline.py:882-884`) afgør på middel-residual med 1 s margin over de fælles
  klip, ofte 6-15 klip.

Brugerfacit viser, at 2,5-4 s-afvigelser ligger inden for Whispers egen fejl: SH_S01E04
22:47-23:47 har ca. 10 segmenter 2,5-4 s "for sent", og scenen er korrekt. C_S03E05's region på
-3,4 s × **12 ankre** er ifølge brugeren en fejlrettelse. medium.en-q5_0 har 3 ankre på -4 s.
turbo har 5 ankre på -3,7 s, der blev skrevet (`sh_all15`). Kravet om 3 ankre beskytter altså
ikke. Selv 12 ankre kan tage fejl.

**Forslag:** Adskil "lokalisér" og "ret". Ankre med residual under ca. 5 s må kun føre til en
note eller til at køre alass/VAD på regionen. En omskrivning kræver, at en uafhængig præcis måling
(alass på regionen eller VAD-onset-match) bekræfter skiftet inden for fx ±0,5 s. Uden bekræftelse
bliver det SUSPECT uden skrivning. Det fjerner turbo-, SH04- og C_S03E05-klassen på én gang og
gælder alle modeller.

### M2. Grenenes rækkefølge skjuler senere detektioner (middel, hul)

`correctness_and_finish` (`pipeline.py:1892-2138`) er én `if/elif`-kæde:
- En succesfuld anker-resync (`pipeline.py:2061-2063`) går direkte til line-order. Jitter,
  manglende midterparti og afkortning tjekkes ikke (kendt). **Verificeret** ved C_S03E03 jitter
  (H4), og i `sh_all15` rammer det turbo E01 missing_middle og cut_version.
- En SUSPECT fra anker- eller blokgrenen skjuler et samtidigt hul. Noten nævner kun det første.
  Et cut_version-afsnit (hul plus forskydning) rapporterer aldrig hullet efter en vellykket
  rettelse.
- Jitter-dommen bruger `escalated_samples` fra "new"-kandidaten (`pipeline.py:1812`), også når
  "old" eller "blocks" vandt (`pipeline.py:2096-2097`). Dommen gælder så en fil, der ikke blev
  skrevet (læst).

**Forslag:** Del funktionen i to faser: (1) vælg og skriv en rettelse, (2) kør **alle**
detektorer (jitter, hul inkl. hoved og hale, restkontrol, trin) på filen, som den ligger nu, og
saml noterne. Rækkefølgen kan så ikke længere skjule noget.

### M3. "VAD-tilt" er Whisper, ikke VAD, og derfor ikke en uafhængig bekræftelse (middel, hul)

`sync.vad_binary` er som standard `""` (`settings.py:608`). `vad.timeline_for_video`
(`vad.py:181-215`) tager det fulde transskript først, derefter klip-cachens segmenter, og kun
som sidste udvej Silero. Den "uafhængige" VAD-tilt, som skal bekræfte fps-rettelsen
(`pipeline.py:1453-1460`), er altså bygget på Whisper-segmentstarter, samme kilde som
anker-tilten. Kvantiserede modeller, der samler tidsstempler, rammer begge ens. `vad.py:189` slår
desuden op uden provider og model, så et gammelt transskript fra en anden model kan bruges (læst).

**Forslag:** Gør Silero til standard, når binæren findes (den bruges allerede i 14.28), og brug
den som præcisionskilde til fps-bekræftelse, restkontrol og regionsjustering.

### M4. Jitter-målet mætter, og store jitter-fejl fanges kun ved et tilfælde (middel, overfitting)

`anchor_jitter` er medianen af ankrenes MAD, men et anker eksisterer kun, hvis dets
MAD ≤ `ANCHOR_MAX_MAD_SECONDS = 1.0` (`subtitles.py:208`, `417`). Målet er altså kappet ved 1 s
og måler de ankre, der overlever. Tærsklen 0,5 s passer til ±1-3 s-scenariet. **Verificeret:**
±2-5 s fanges i proben via anker-residualer, ikke via jitter-reglen, og efter at filen er
omskrevet. ±0,5-1,5 s: 8/18 tavse, alle omskrevet.

**Forslag:** Mål jitter på de rå `anchor_points` (alle matchede linjer pr. klip, også i klip der
ikke blev til et konfident anker). Fx andelen af linjer, der afviger over 1 s fra klippets median,
over hele puljen. Ved jitter-dom: rul alass' skrivning tilbage (H4).

### M5. Hvis eskaleringen fejler, behandles filen tavst som ok (middel, hul)

Hvis `collect_samples_full` returnerer "skipped" (`pipeline.py:1763`), beholdes sampled-evidensen.
Jitter- og hul-dommene kræver fuld dækning (`pipeline.py:2096-2109`) og springes over, så filen
ender "ok", selv om triggeren netop havde set et mistænkeligt hul på 120 s+ (læst). Det samme
gælder `_missing_middle_hit` uden cachet transskript (`pipeline.py:1543`).
**Forslag:** Når en trigger fyrede, men bekræftelsen ikke kunne køre, skal flaget være "unknown"
med en note, ikke "ok".

### M6. Fremmedsprogede undertekster har ingen tidskontrol overhovedet (middel, hul)

`anchors_applicable` (`subtitles.py:324-335`) slår alle ankre fra, når undertekstens sprog er et
andet end talens. Så forsvinder screen, anker-escalation, resync, fps, jitter og restkontrol. Kun
alass og indholds-scoren står tilbage, og flaget bliver alligevel "ok". Brugeren ser den danske
fil (14.19) (læst).
**Forslag:** Skriv "ok (tidskontrol ikke mulig)", så det ikke ligner en bestået dom. Brug den
verificerede engelske undertekst til samme video som tidsreference for den danske (alass kan
aligne undertekst mod undertekst, og cue-struktur kan sammenlignes), eller brug VAD-onset-match,
som ikke afhænger af sprog.

### M7. `anchor_slope_breaks` kan kun se trin ≥ 6-18 s (middel, overfitting)

`SLOPE_BREAK_MIN_DEV = 0.30` (`correctness.py:681`) er trin/Δt. Med ankre 20-60 s fra hinanden
kræver det trin på 6-18 s, præcis størrelsen af de indlagte blokke på ±5-15 s. En resterende blok
på 3-5 s giver aldrig et brud (læst, jf. probe: pw_small 4/12 delvist og tavse).
**Forslag:** Brug den mekaniske grænse, altså den største plausible rate (`STRETCH_MAX_RATE`,
0,08), i stedet for 0,30, og behold trinbarren (fx 3 s). Det skal måles mod clean og drift.

### M8. Matrixens scenarier er for pæne og har formet tærsklerne (middel, overfitting og testkvalitet)

- piecewise er altid 6 lige store blokke, alle ±5-15 s, uden ren reference-del. "Filen er rigtig
  undtagen én scene" findes ikke, og det er netop den, der fejler (H1).
- missing_middle er altid 300 s ved 40 %. cut_version er altid 300 s. Drift er kun 2 % og 0,1 %.
  Jitter er kun ±1-3 s. Der er intet scenarie med afkortning, blok nær kanten eller mellem-rate.
- Kaskade-kalibreringer: `SCREEN_MIN_AGREE_FRAC` ("clears piecewise by 0.07"),
  `FPS_ANCHOR_TILT_MIN_S` (hævet efter én gap-FP), `RAMP_RESCUE_*` ("calibrated on 2 configs"),
  `ANCHOR_RUN_MIN_DEV_S` (3 matrix-rækker), `JITTER_MIN_MAD_S` og `MISSING_MIDDLE_MIN_WORDS` er alle
  sat midt mellem et indlagt scenarie og det raske maksimum. Det er mekanisk forsvarligt for det
  indlagte scenarie og skrøbeligt for alt andet.
- `recovered` måles med frac ≤ 0,5 s eller ≤ 1 s, som skjuler Whisper-bias på 0,2-0,45 s skrevet
  ind i filen (H3). `NO_CHANGE_SCENARIOS` indeholder stadig `uniform_p03`, selvom
  `min_change` nu er 0,25, så 80 % af de rækker tælles som "rørt" uden at være fejl.

**Forslag:** Tilføj scenarier, der trækkes fra fordelinger og ikke fra faste værdier: hul 60-300 s
på tilfældig position inklusive start og slut, én til to blokke på 1-5 min med ±1-8 s i en ellers
ren fil, drift 0,15-1 %, 25->23,976 fps, jitter 0,3-5 s og kombinationer (PAL + hul, blok + drift).
Mål rettelse som p50 < 0,15 s og p99 < 0,5 s.

---

### L1. Cachens payload taber `fps_points` og `full_coverage` (lav, bug)

`pipeline.py:1783-1788` og `1861-1866` gemmer ikke `fps_points` og `full_coverage`. Ved
genbrug (`pipeline.py:1708-1712`) læser `_fps_says_needs_full` `collected["fps_points"]` direkte
(`pipeline.py:1502`) og kan aldrig udløse. En eskaleret full-pulje genbruges som om den var
sampled (læst).

### L2. `cue_gaps` bruger forrige cues slut, ikke det løbende maksimum (lav, bug)

**Verificeret:** en lang cue 0-100 s med en kort cue 10-12 s indeni og næste cue ved 130 s giver
hullet (12, 130), altså 118 s i stedet for 30 s. Rettelse: brug `max(end)` over alle tidligere cues.

### L3. `evaluate_against_full_transcript` filtrerer ikke gentagelsesløkker (lav, inkonsistens)

`correctness.py:948` dropper kun nonspeech, mens `full_transcript_for_check` også dropper
løkker. Resync-planen og kandidatvalget ser derfor en anden evidens end dommen.

### L4. Docstrings modsiger standardværdierne (lav, oprydning)

`_try_fps_rescale` og `_fps_says_needs_full` siger, at fikset kræver fuld dækning, men
`fps_require_full_coverage` er False (`settings.py:650`), så sampled evidens med den "flippy"
sparsomme VAD (se docstringen) kan rette. Enten slås den til, eller docstrings og risikovurdering
opdateres.

### L5. Ufærdige tests i tests/ (lav)

`tests/test_drift_e06.py` og `tests/test_resync_coverage.py` er ikke tracket. drift_e06 fejler
13/13 på HEAD (`_late_presync_gates_pass` findes ikke), så `pytest tests/` er rød uden grund.

---

## 2. Tærskler: mekanisme eller tilpasning (spørgsmål 1)

| konstant | sted | vurdering |
|---|---|---|
| `ANCHOR_SUSPECT_THRESHOLD_S` 2,5 | subtitles.py:223 | Mekanisk som støjgulv, men brugt som **skrive**-trigger. Brugerfacit (SH04, C_S03E05) viser Whisper-fejl på 3-4 s. Bør kun lokalisere. |
| `ANCHOR_SUSPECT_MIN_SAMPLES` 3 / `anchor_suspect_min_samples` 2 | correctness.py:649, settings.py:637 | Et antal uden hensyn til tæthed og placering. 2 af 51 er støj, men 2 af 2 i en blok er hele blokken (H1). Brug klynger/nabokrav. |
| `ANCHOR_HUGE_SINGLE_S` 10 | pipeline.py:746 | Rimelig (4× støj) og kun en eskaleringstrigger. Ufarlig. |
| `anchor_run_offsets` k 10 / 1,2 s | correctness.py:781-782 | Tilpasset 3 matrix-rækker. Kører kun efter resync. Ved 60 s-interval ses kun rester over ca. 6 min. |
| `SLOPE_BREAK_MIN_DEV` 0,30 | correctness.py:681 | Tilpasset de indlagte blokstørrelser (M7). |
| `JITTER_MIN_MAD_S` 0,5 / 0,4 | correctness.py:709-712 | Tilpasset ±1-3 s. Mætter ved 1 s via `ANCHOR_MAX_MAD` (M4). |
| `MISSING_MIDDLE_*` 90 s / 200 ord / 120 s | correctness.py:732-737 | Tilpasset 300 s ved 40 % mod titelsangen. Kurven i H5. |
| `SCREEN_TOLERANCE_S` 0,25 | pipeline.py:176 | Lig med min_change, men under Whispers bias (H2). |
| `SCREEN_MAX_TAIL_GAP_S` 150 | pipeline.py:190 | Mekanisk begrundet (55 s rask), men uden dom bagefter (H6). |
| `SCREEN_MIN_AGREE_FRAC` 0,80 | pipeline.py:186 | Eksplicit tilpasset ("clears piecewise by 0.07"). |
| `FPS_*_TILT_MIN_S` 0,9 / 0,9 / 0,7 / 0,3 | subtitles.py:253-256 | Tilpasset 4 filer og 1 FP. Målt i sekunder, altså afhængig af længden: på et 10-min afsnit er ægte 0,1 % kun 0,6 s og kan aldrig rettes, på en film er den let. |
| `FPS_RATIOS` kun 1001/1000 | subtitles.py:246 | Mangler en konsistenskontrol af raten (H3b). |
| `STRETCH_MIN_TILT_S` 8 | subtitles.py:303 | Absolut, så der opstår en død zone (H7). |
| `STRETCH_MIN_KEEP_FRAC` 0,9, `RAMP_RESCUE_*` | subtitles.py:317, pipeline.py:762-764 | RAMP er eksplicit "calibrated on 2 configs". Keep 0,9 hviler på en ablation (914 puljer), det er fint. |
| `FPS_RESID_MAX_S` 1,5 | subtitles.py:321 | Selvkonsistens på samme pulje og ikke uafhængig. Lod en forkert ratio passere (H3). |
| `QUARTILE_MIN_POINTS` 5 | subtitles.py:588 | Mekanisk. OK. |
| `ANCHOR_REGION_MIN_ANCHORS` 3 | subtitles.py:802 | For lavt til at skrive (M1). |
| `ANCHOR_RESYNC_MIN_SHIFT_S` 1,0 | subtitles.py:806 | Gælder kun "alle regioner". Små regioner skrives med bias (H3d). |
| `SWAP_MARGIN` 0,15 | line_order.py:74 | Kun rapportering. Lav risiko. Verdikten er timing-afhængig: på en fejl-timet fil giver line-order støj (40 "swaps" på C_S03E03 drift03). |

## 3. Performance på N100 (spørgsmål 6)

- Hul-triggeren køber hele afsnittets transskript for et naturligt hul på 120 s+ (SH E01/E02
  altid, 4 dropdup-rækker). Muse målte ca. 198.000 friske lydsekunder over 70 rækker. Forslag
  H5c: transskribér kun hullet.
- Jitter-triggeren kunne på samme måde nøjes med 4-6 ekstra klip i stedet for fuld dækning.
- Forslagene i H1/H2 (kør alass oftere) koster alass-tid plus lydudtræk (ca. 6,5 s her, flere
  gange det på N100). Det er stadig langt billigere end ét fuldt transskript, og alass er den
  præcise del.
- `correctness_and_finish` er ca. 500 linjer med rækkefølgeafhængige grene. Tofaseopdelingen (M2)
  mindsker også risikoen ved fremtidige regler.

## 4. Testkvalitet (spørgsmål 5)

- Pipeline-testene bruger `frac_le_1_0s >= 0.90` som bestået. Det tillader, at 10 % af cues er
  over 1 s forkerte, og fanger hverken bias-omskrivninger (0,2 s) eller PAL-rest (0,3 s).
- `test_anchor_jitter.py:21-22` fastlåser et sampled-maksimum på 0,445, der ikke længere dømmes
  (jitter dømmes kun på fuld dækning), og tester i praksis kun median-aritmetik.
- Hul-detektionen testes kun på SH_S01E01 med 300 s ved 40 %, det mest gunstige tilfælde. Der
  mangler tests for 120/150 s, huller ved kanterne, afkortning og indlejrede cues (L2).
- Der er ingen test af, at "old" kan vinde en single-block-sammenligning (H4). Der er ingen test
  af, at en SUSPECT-dom ikke efterlader en forværret fil. Der er ingen test med en blok i en ellers
  ren fil. Der er ingen test af fps-ratioens konsistens.
- De sporede tests blev kørt (`pytest_tracked.log` i scratchpad, uden matrix-testene): 247
  bestået, 0 fejlet. De er altså grønne, mens proben viser de tavse gennemløb ovenfor. Testene
  dækker de indlagte scenarier og ikke varianterne af dem.

## 5. Anbefalet rækkefølge

1. H4 (skriv ikke det ankrene modbeviser, og rul tilbage ved SUSPECT). Det er billigst og
   stopper ødelæggelser.
2. H1 og H2 (screen-ok må ikke vinke blokke og små offsets igennem, kør alass).
3. H3 og M1 (Whisper lokaliserer, alass og VAD retter; snap rater; regioner under 1 s får 0).
4. H6 og H5 (hoved/hale-huller, ♪-filter og lavere barre, målrettet transskription af hullet).
5. H7 og H8 (rate-gate efter signifikans og en endelig restkontrol).
6. Udvid matrixen (M8) og kør på tiny.en-greedy først, derefter alle modeller til bekræftelse.
