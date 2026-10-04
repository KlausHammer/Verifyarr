# Afsluttende review — verifyarr

Dato: 2026-09-26. Gennemgået: HEAD `42766ad` (+ `77f3d34`, `d34384e`). Ingen kode er ændret, committet eller pushet.
Målinger: SH_S01E01, `tiny.en-greedy-cpu` (medmindre andet står), denne PC (WSL, video på `/mnt/c`).
Artefakter (profiler, cProfile-filer, repro-scripts): `rapport/afsluttende_review_profiler/`.

---

## 1. Kort resumé

1. **KRITISK: produktionens standardopsætning giver falske SUSPECT på korrekte SH-filer.** `docker-compose.yml` sætter `WHISPER_MODEL: small.en-q5_1`, og `sync.vad_binary` er som standard `""`. Uden VAD bliver punkt-run-blokdetektoren (kalibreret til tiny) ikke bekræftet af noget. Målt, clean, full mode, VAD fra: small.en-q5_1 SH_S01E02 **SUSPECT** ("26:36-27:03 (-2.2s)") og turbo-q5_0 SH_S01E01 **SUSPECT**. Samme fil med VAD tændt: ok. Matrixen tester aldrig konfigurationen uden VAD.
2. **KRITISK: en genkørsel af en uændret undertekst mister arm-2-detektionen.** Line-order-cachen gemmer ikke `full_coverage`/`fps_points`. Målt: missing_middle i full mode giver **SUSPECT i kørsel 1 og "ok" i kørsel 2** på samme DB. Det rammer Rescan (force) og alle filer, hvor mtime ændres uden at indholdet gør.
3. **HØJ ydelse: VAD dekoder hele lydsporet igen for hver fil.** Det tager 27-35 s af en række på 34-44 s (80-97 %). I sampled mode sker det selv på sunde filer, hvor screenet netop sprang alass over for at undgå den udtrækning. alass' WAV ligger allerede i `audio_cache_dir`, men bliver ikke genbrugt.
4. **HØJ: generate-stien crasher igen i rapporten.** `_pre_sync_subs`/`_orig_subs` (pysubs2-objekter) lækker til `write_report`. Det er samme fejlklasse som den, der tidligere blev rettet for `_ambiguous_sync`.
5. **HØJ: Bazarr-hooket (`verifyarr.py single`) kører uden om webappens job-lås.** Det kan behandle samme fil samtidig med en sweep og kører dobbelt Whisper på N100 (sandsynligt race).

Python-CPU er ikke flaskehalsen på almindelige afsnit: ca. 0,6-0,8 s pr. række på denne PC. De store poster er subprocess-I/O (ffmpeg/VAD/ffprobe) og Whisper-lyd (450 s sampled, +249 s gap-probe, 3232 s ved eskalering). Theil-Sen bliver først mærkbart på film.

---

## 2. Fund i prioriteret rækkefølge

### KRITISK

**K1. Standard-config (VAD fra, compose-model small.en) → falske SUSPECT fra punkt-run-detektoren**
- Hvor: `verifyarr/settings.py:608` (`"sync.vad_binary": ""`), `docker-compose.yml:13` (`WHISPER_MODEL: small.en-q5_1`), `verifyarr/pipeline.py:1653-1655` (`if not ivs: return runs`, så runs beholdes ubekræftede), `verifyarr/correctness.py:830-838` (kommentar: "Model-bound: other models run 2-12s on healthy SH").
- Bevis: `afsluttende_review_profiler/novad_fp.py` (6 SH clean, full, VAD fra):
  - tiny.en-greedy: 6/6 ok.
  - small.en-q5_1: SH_S01E02 SUSPECT "Part of the episode is out of sync: 26:36-27:03 (-2.2s)".
  - turbo-q5_0: SH_S01E01 SUSPECT "20:59-21:23 (-3.6s), 31:01-32:41 (-4.4s)".
  - base.en-greedy: 0.
  - small.en-q5_1 SH_S01E02 **med** VAD: "block run ... not confirmed by VAD" → ok.
- Hvorfor: SH er facit, så ethvert flag der er en FP. Detektoren er kun sikker med tiny, eller hvis VAD bekræfter. Ingen af delene er standard i produktion. `e2e_matrix.cfg_for` (tests/e2e_matrix.py:362-364) tænder altid VAD lokalt, så denne konfiguration er aldrig målt.
- Forslag (i rækkefølge):
  - (a) Uden VAD-tidslinje skal detektoren tie (`return []`) i stedet for at beholde ubekræftede runs. Det svarer til "fail closed", som resten af projektet gør.
  - (b) Gate `POINT_RUN_*` på modellen ligesom `missing_middle_min_words` (kun tiny), eller kalibrér pr. model.
  - (c) Ret `docker-compose.yml` til `tiny.en`. Dockerfilens `ARG WHISPER_MODEL=tiny.en` og `settings.py:36` siger allerede tiny.
  - (d) Sæt `vad_binary=/usr/local/bin/whisper-vad-speech-segments` som default i Docker. Binæren er bagt ind, og modellen skal stadig mountes, så (a) skal med under alle omstændigheder.
  - (e) Tilføj en matrix-arm med `vad_binary=""`.
- Risiko ved rettelsen: (a) koster blokdetektion uden VAD. Mål på block_rand* med VAD fra, før (a) vælges frem for (b).

**K2. Cache-genbrug mister `full_coverage` → missing-middle/jitter-dom forsvinder ved genkørsel**
- Hvor: cachen skrives uden `full_coverage`/`fps_points` i `pipeline.py:2021-2026` (også 2149-2154 og 2373-2378). Den læses i `pipeline.py:1940-1944`. `line_order.py:863-864` sætter `full_coverage=False`, når nøglen mangler. Dom-grenene afhænger af flaget: `pipeline.py:2412-2413` (jitter), `pipeline.py:2423-2428` (missing middle: `... if full_coverage or escalated else (probe if sampled else None)`, så full mode giver `None`).
- Bevis: `afsluttende_review_profiler/rerun_cache.py missing_middle full` (samme DB, samme undertekst):
  - `RUN1 flag=SUSPECT` "Subtitle has no lines for 304 s at 21:10-26:14 …"
  - `RUN2 flag=ok`
  - Sampled mode flipper ikke, men betaler gap-proben (Whisper) igen.
- Hvorfor: arm 2 skal detektere 100 %. Et SUSPECT bliver stille overskrevet med ok i DB. Det sker ved Rescan (`force`, `jobs.py:325`) og altid når `should_skip` fejler på mtime/størrelse (`db.py:466-468`), fx hvis Bazarr henter den samme fil igen.
- Forslag: gem `full_coverage` og `fps_points` i cache-JSON'en (én helper `_cache_json(collected)` i stedet for de tre kopier), og læg en skemaversion ind i `cache_key_for` (`line_order.py:151`), så gamle rækker ugyldiggøres. Test: kør samme række to gange, og dommen skal være ens.

### HØJ

**H1. VAD-udtrækning af hele lydsporet pr. fil (dominerende tidspost)**
- Hvor: `vad.py:273-294` (`speech_timeline` → `extract_audio_wav` til en temp-WAV, som smides væk). Kaldes fra `pipeline.py:1748` (`_vad_says_needs_full`, sampled, alle ikke-eskalerede filer) og `pipeline.py:1653` (`_block_runs_hit`).
- Målt (cProfile, `afsluttende_review_profiler/*.txt`):

| Række | Wall | speech_timeline | heraf ffmpeg | Silero |
|---|---|---|---|---|
| sampled clean | 33,9 s | 31,8 s | 26,8 s | 5,0 s |
| sampled hole_rand0 | 33,4 s | 31,2 s | 26,7 s | 4,5 s |
| sampled block_rand0 | 36,2 s | 31,5 s | 26,9 s | 4,6 s |
| full block_rand0 | 44,4 s | 39,8 s | 35,1 s | 4,6 s |
| full clean / drift (ingen VAD-kald) | 1,6-3,9 s | – | – | – |

- Hvorfor: SH-filen er 4,3 GB. ffmpeg læser hele containeren. På en N100 over gigabit-NAS er det mindst ca. 40 s alene i I/O (sandsynligt, ikke målt). `_screen_pair`s docstring (`pipeline.py:227-231`) begrunder netop screenet med at undgå denne udtrækning, men VAD-triggeren betaler den alligevel på den sunde fil.
- Forslag:
  1. Genbrug alass' WAV. `resolve_alass_reference` har den i `audio_cache[video]` (`sync_engine.py:164-173`). Send stien med til `speech_timeline`, eller slå `audio_cache` op først.
  2. Persistér VAD-intervallerne i SQLite, keyet på (video, mtime, size) ligesom transkript-cachen. VAD er uafhængig af sprog og undertekst, så anden sprogfil, Rescan og næste sweep bliver gratis.
  3. Når der ikke findes en WAV, så dekod kun lyden med `-map 0:a:0`. Det sker allerede (`-vn`), men I/O'en er den samme. Det reelle alternativ er 1 og 2.
- Gevinst: 27-35 s pr. fil på denne PC, når VAD er slået til. Med persistering er gevinsten pr. genkørsel/sprog også ca. 5 s Silero.
- Relateret: `_VAD_MEMO[key] = out` gemmer også `None` (`vad.py:293`), så en forbigående fejl (NAS-hikke, timeout) slår VAD fra for den video, indtil containeren genstartes. Det kobler direkte til K1, fordi runs så beholdes ubekræftede. Temp-WAV'en memoiseres desuden under sin tilfældige temp-sti (`vad.py:269`, kaldt fra `vad.py:292`). Den post rammes aldrig igen og er et rent læk i en langtlevende proces.

**H2. Generate-stien: pysubs2-objekter i rækken → `write_report` fejler**
- Hvor: `pipeline.py:736-738` sætter `row["_pre_sync_subs"]` (og `_orig_subs`) i den direkte skrive-sti. `jobs.py:498-500` kalder `sync_pair(..., defer_verification=False)` → `finish_generated` (`pipeline.py:370-417`), som kun popper `_ambiguous_sync`, og derefter `write_report` → `json.dumps(row)`.
- Bevis: `json.dumps({'_pre_sync_subs': pysubs2.SSAFile()})` giver `TypeError: Object of type SSAFile is not JSON serializable`. Regressionstesten (`tests/test_generate_persistence.py:66-71`) dækker kun `_ambiguous_sync`.
- Hvorfor: en generering, hvor alass flytter over 0,25 s (typisk for Whisper-tider), bliver registreret som en fejlet kørsel. Filen er skrevet og DB opdateret. Det er præcis den fejl, testfilens docstring beskriver som "shipped".
- Forslag: én `_strip_private(row)`, der popper alle `_`-nøgler, kaldet i `finish_generated`, `correctness_and_finish` og `write_report` (defensivt). Test: generate-rækken efter en direkte alass-skrivning skal kunne serialiseres til JSON.

**H3. CLI `single` (Bazarr-hook) kører uden om `JobRunner`-låsen**
- Hvor: `cli.py:57-79` → `_execute_command` → `jobs.execute_run` direkte. Låsen findes kun i webprocessen (`jobs.py:505-591`).
- Hvorfor (sandsynligt, ikke reproduceret): Bazarr-pollet (`scheduling.poll_new_media_enabled`, standard til) starter en sweep, når et ønsket element er løst. Samtidig kører Bazarr sit post-processing-hook på samme nye fil. Det giver to processer på samme undertekst, dvs. dobbelt skrivning/backup og en uforudsigelig DB-række. Samtidig kører to whisper-cli med 4 tråde hver på 4 N100-kerner.
- Forslag: en fillås (fx `fcntl.flock` på `/data/verifyarr.job.lock`, eller en lås pr. undertekst-sti) i `execute_run`, eller lad CLI'en poste til webappens API, så den kommer i kø.

**H4. `extract_audio_wav` timeout 180 s er for kort til store filer på N100/NAS (sandsynligt)**
- Hvor: `sync_engine.py:49` (`timeout: int = 180`). Bruges af alass-referencen og af VAD (`vad.py:292`).
- Hvorfor: 4,3 GB tog 27-35 s her (ca. 130-160 MB/s). En 20-30 GB remux over gigabit (ca. 110 MB/s) bruger 180-270 s alene på I/O. Konsekvens: VAD bliver stille `None` og memoiseres (H1 → K1-FP-stien), og alass falder tilbage til videoen direkte, hvilket er langsommere.
- Forslag: skalér timeout med filstørrelse (fx `max(180, size_GB * 30)`) og log en WARNING ved timeout.

### MIDDEL

**M1. Gap-proben i sampled mode koster +55 % Whisper-lyd på sunde filer og caches ikke**
- Hvor: `pipeline.py:1564-1591`.
- Målt: sampled clean `fresh_audio_s 699,1` mod 450 s for screen-klippene alene, altså **+249 s** (head/tail/intro-huller). Uden begrænsning, fordi en sund pause aldrig når baren, så hele hullet transskriberes. Kommentaren `correctness.py:749` angiver "4-6 min audio per episode". Transskriptionerne gemmes ikke i `video_transcript_cache`, så anden sprogfil og Rescan betaler igen. `whisper_cost.fresh_s += dur` (`pipeline.py:1578`) tælles også, når udtrækning eller transskription fejler.
- Forslag:
  - Gem probe-klip i klip-cachen (samme nøgle som collect_samples).
  - Medregn allerede transskriberede screen-klip, der ligger i hullet.
  - Spring huller over, hvor VAD (når den findes) viser under `MISSING_MIDDLE_MIN_SPEECH_S` tale.
  - Sæt et loft pr. fil (fx 300 s).
- Gevinst: ca. 250 s tiny-lyd pr. sund fil ved genkørsel. På N100 sandsynligvis ca. 30-60 s CPU pr. fil (5800X3D: tiny greedy ca. 20× realtid ifølge `sweep/tider_cpu.csv`; N100 anslået 3-4× langsommere, ikke målt).

**M2. Samme fulde transskript læses og filtreres 6-10 gange pr. fil, og ffprobe kaldes 3-7 gange**
- Hvor: `pipeline.py:1616-1627`, `1637-1644`, `1668-1676` (4× i `_try_rate_from_baseline:1694-1713`), `1762-1771`. Hvert kald laver `db.get_full_transcript_cache` + JSON-parse + `_drop_repetition_loops(_drop_nonspeech(...))`.
- Målt:
  - `get_full_transcript_cache` 6-10×/række.
  - `_ffprobe_json` 4-7×/række, 0,23-0,56 s (`get_duration_seconds` 2-4×, `detect_audio_language_ffprobe` 2-3×, `correctness.py:93`, `line_order.py:710-713`).
  - `collect_samples_full` 2× i full mode (screen `pipeline.py:249` + `pipeline.py:1953`), 0,57-0,82 s.
  - WARNING "Dropped 4 segment(s) from Whisper repetition loops" logges 4-6× pr. fil (`generate.py:644`).
- Forslag: et lille per-fil-kontekstobjekt (eller `functools.lru_cache` på (video, mtime, provider, model)) til filtrerede segmenter, varighed og ffprobe-sprog. Genbrug screenets `collected`, når `current_subs is old_subs` (screen "ok").
- Gevinst: ca. 0,5-1,2 s pr. fil her (anslået 1,5-3 s på N100), plus 4-5 færre identiske WARNING-linjer pr. fil.

**M3. Theil-Sen O(n²) på tætte pools skalerer dårligt til film**
- Hvor: `subtitles.py:510-541` (`robust_rate_fit`), `subtitles.py:452-463` (`_theil_tilt`), `subtitles.py:681-738` (`anchor_drift_signature` = 10 Theil-Sen-fits).
- Målt (syntetisk, denne PC):

| n punkter | stretch_probe | anchor_drift_signature |
|---|---|---|
| 300 | 0,009 s | 0,05 s |
| 600 | 0,04 s | 0,23 s |
| 1200 | 0,18 s | 1,15 s |
| 2000 | 0,69 s | 3,63 s |

- SH (ca. 500 linjer) koster 0,05-0,15 s i alt, så det er ubetydeligt. En 2-timers film (1200-2000 matchede linjer) × 4 dense-probes i rate-trinnet giver 0,7-2,8 s her, anslået 2-9 s på N100.
- Forslag: tynd deterministisk ud til ≤ 400 punkter (stride, som VAD allerede gør med `stride=3`), eller brug en O(n log n)-median-slope. Gevinst 10-25× på film. Kræver en matrix-validering af rate-gates (estimatet ændrer sig lidt).

**M4. Dom-kæden: huller i rækkefølge og tilstand**
- a) Efter en vellykket anchor-resync (`pipeline.py:2251-2378`) tjekkes hverken missing middle eller jitter. En fil med hul, hvor resync planlægger én region, bliver "ok" (kodelæsning; ikke set i matrixen, fordi cut_version planlægger ≥ 2 regioner og derfor flagges). Forslag: kør de rene detektorer (missing middle, jitter) efter alle skrive-grene.
- b) Rate-rettelsen (`pipeline.py:2128-2137`) nulstiller ikke `sync_split_blocks`/`sync_block_spread_s` fra en 'blocks'-vinder, selvom filen nu er genskabt fra originalen. Sikkerhedsnettet (`pipeline.py:2190-2195`) kan så fyre på forældede felter (latent: 0 rækker i bk1). Forslag: nulstil dem i fps_fix-grenen.
- c) `_block_repair_parts` (`pipeline.py:1594-1597`) parser menneskelig statustekst. Den direkte multi-blok-skrivning i `sync_pair` (`pipeline.py:731-740`, når `--no-split` fejler) får status "fixed (Δx s)" → `_block_repair_parts(...) == 0` (verificeret), så commit 42766ad's regel ("alle blokreparationer i ≥ 2 dele flagges") rammer den ikke. Forslag: brug det strukturerede felt `row["sync_split_blocks"]`/antal planregioner.
- d) Rate-trinnet har intet indholdstjek (`_try_rate_from_baseline`, `pipeline.py:1692`), modsat `_try_fps_rescale:1472` ("never retime what content-matching didn't verify"). Matrixen viser wrong_episode 24/24 urørt (gates redder det), men tilføj `result["flag"] == "ok"` for konsistens.

**M5. `evaluate_against_full_transcript` filtrerer ikke repetition-loops**
- Hvor: `correctness.py:1099` (kun `is_nonspeech_annotation`). `collect_samples_full` (via `generate.full_transcript_for_check:1130/1139`) og de nye helpers bruger begge filtre, og kommentaren i `pipeline.py:54-56` siger, at det er nødvendigt ("an unfiltered repetition loop reads as a scene's worth of dialogue").
- Hvorfor: `_resolve_ambiguous_sync` og `_try_anchor_resync` i full mode dømmer kandidater på ankre fra loop-segmenter. Forslag: brug den fælles filtrerede kilde fra M2.

**M6. Disk: alass-WAV'er holdes hele sweepen og efterlades ved crash**
- Hvor: `jobs.py:320-321` (én `TemporaryDirectory` for hele sweepen), `sync_engine.py:164-168`.
- 16 kHz mono = 1,92 MB/min, dvs. ca. 86-100 MB pr. SH-afsnit. En Rescan af 200 afsnit fylder op til ca. 17-20 GB i containerens `/tmp` (overlay på værtens rodfilsystem). Ved kill/genstart bliver `/tmp/verifyarr-audio-*` liggende.
- Forslag: slet en videos WAV, når ingen resterende par i `to_process` bruger den (refcount), men først efter correctness-fasen (så VAD kan genbruge den, jf. H1). Ryd forældede `verifyarr-audio-*` ved opstart.

**M7. Jitter-filer omskrives og bliver værre på 2/6 afsnit (seneste matrix bk1, 20:37, efter 77f3d34)**
- 20/24 jitter-rækker er "fixed (Δ1,9-3,7s)". SH_S01E03: p50 1,99 → 2,24 s. SH_S01E04: 2,05 → 3,06 s. Alle er flagget SUSPECT, så detektionen holder, men harnessens "ingen ændring"-kontrakt (`e2e_matrix.py:162`) brydes. Anchor-resync/alass skriver en rettelse på en fil, som jitter-detektoren bagefter dømmer.
- Forslag: kør jitter-dommen (fuld dækning) før en resync-skrivning, og lad den vinde (kun detektion, intet skrives).

**M8. Testhuller for de nye trin**
- Ingen test refererer til: `_try_rate_from_baseline`, `_dense_pool`, `dense_anchor_points`, `_block_runs_hit` (VAD-bekræftelse og `proven_block`), `shift_fits_speech`, `speech_timeline`, `_missing_middle_probe` (early stop, omkostning), `_missing_middle_hit`, `all_gaps`, `gap_probe_windows`, `_vad_says_needs_full`, `_rate_says_needs_full`, `_block_repair_parts`, `_swap_gate_hit`, `swap_gate_trips`, `apply_pending_sync`, `_ramp_rescue_probe`, `_try_stretch_rescale` (grep i `tests/test_*.py`).
- Mangler også: en genkørsel med cache-hit (K2), en matrix-arm uden VAD/anden model (K1) og JSON-serialisering af generate-rækken efter direkte skrivning (H2).
- De rigtige-data-tests (`test_silent_rows`, `test_fps_guard`, `test_drift_ramp`, `test_swap_gate`, `test_sh04`, `test_fix_61_62`, `test_fps_rescale`, `test_screen_order`) springes stille over uden `/mnt/c/.../whisper_gpu_staging`. Det betyder grøn uden dækning på enhver anden maskine eller i CI.
- De er også de langsomme. Hver ny video koster en fuld VAD-udtrækning (H1). Med H1-persistering eller en fixture-VAD-TSV (`vad.parse_vad_tsv` findes allerede) bliver de minutter hurtigere.
- Testvarigheder og tempfil-læk (3,7 GB i `/tmp`) står i tillægget.

**M9. `correctness_and_finish` er 588 linjer (`pipeline.py:1892-2479`), `_resolve_ambiguous_sync` 369 linjer (`812-1180`)**
- Forslag til opdeling uden adfærdsændring:
  1. `_gather_evidence()` (1933-2010: cache/indsamling + eskaleringsstigen som en liste af `(navn, predikat)`).
  2. `_swap_gate_verdict()` (2039-2061).
  3. `_resolve_and_veto()` (2069-2105).
  4. `_apply_rate_fixes()` (2114-2154).
  5. `_verdict_chain()` som en eksplicit ordnet liste af detektorer, der returnerer `(flag, note)` eller `None`. Det gør rækkefølgen testbar og løser M4a.
  6. `_finish()`.
- Saml de 8 identiske `handle_suspect(...)`-kald (fx `_suspect(row, note)`), de 3 cache-JSON-kopier (`2021`, `2149`, `2373`) og de 5 lokale `import copy as _copy` (`315`, `805`, `1414`, `1505`, `1702`).

### LAV

- **L1 Død kode/importer**: se afsnit 4.
- **L2 Forældede kommentarer og dokumentation**:
  - `settings.py:673-674` siger, at `WHISPER_MODEL`-default er "small.en-q5_1", mens koden (`settings.py:36`) siger tiny.en.
  - `docker-compose.yml:10-13` påstår "small.en-q5_1 ships pre-baked", men Dockerfile:46 bager tiny.en.
  - `line_order.py:807-811` (finalize-docstring) siger, at swaps "gets auto-fixed", mens `pipeline.py:1852-1865` siger "Reports only".
  - `pipeline.py:2107-2113`-kommentaren beskriver kun `_try_fps_rescale`, men rate-trinnet kaldes først.
  - `pipeline.py:1383-1387` siger, at stretch-stien "fires 0 times". Det gælder stadig (bk1: stretch-stien 0/732 og diskret fps 0/732, rate-trinnet 62, presync-rate 84, ramp rescue 16).
  - README (`README.md:9-10, 27-28`) nævner kun Groq/OpenRouter-Whisper, mens lokal Whisper er standard. VAD-modellen, tiny.en og detektionerne (hul, blok, swap) nævnes ikke.
  - `tests/e2e_matrix.py:20` ("under the 0.5s decision bar; nothing may move") modsiger `e2e_matrix.py:963-965` ("expected to move"), og `uniform_p03` står stadig i `NO_CHANGE_SCENARIOS` (`:162`). I bk1 flyttes 20/24 uniform_p03-rækker korrekt og tælles derfor som fejl.
- **L3** `tests/test_block_hole.py:208-209` har `unittest.main()` midt i filen. `PointRunTests` defineres efter og kører ikke ved `python tests/test_block_hole.py` (pytest finder dem). Ubrugt `import copy` (`:8`), ubrugt `subs` (`:53`, `:60`).
- **L4** VAD-subprocess kører uden `wrap_low_priority` og med hårdkodet `threads=4` (`vad.py:240-243`), mens alle andre tunge kald er nice/ionice'et. `block_witness` er ren Python: 0,9 s pr. fil her (`vad.py:325-366`, 11k `_score`-kald), så en numpy-fri stride eller et prefix-sum over masken giver ca. 10×.
- **L5** `whisper_cost.cached_s` tælles dobbelt i full mode (screen + correctness): 6463,6 s rapporteret for et 3231,8 s transskript (`line_order.py:726`).
- **L6** Rate-trinnets "second look" (`pipeline.py:1709-1710`) kører `snap_rate` på `p_full` uden `rate_gates_pass` (kun keep_frac-tjek). Den efterfølgende "flat after" fanger det, men gate'n bør være eksplicit.
- **L7** `_VAD_MEMO` er ubegrænset (`vad.py:47`), plus temp-sti-lækket fra H1.
- **L8** `pipeline.py:56` importerer private `generate._drop_nonspeech/_drop_repetition_loops`. Flyt dem til `subtitles.py`/en `transcript.py`.
- **L9** De nye tætte detektorer ignorerer `anchors_applicable` (`pipeline.py:1644`, `1675`). For danske undertekster kan rene navne-linjer ("Jackson Lamb") matche. Det fejler i praksis lukket (for få punkter), men er inkonsistent med `evaluate_*`. Bemærk også: rate-trinnet kan slet ikke rette danske filer (ingen ankre), så arm 1 for ikke-engelske undertekster hviler på alass alene og er umålt i matrixen.
- **L10** `Config.state_db` sættes (`settings.py:367`) men læses aldrig.
- **L11** SQLite får aldrig `VACUUM`. Cache-JSON er ca. 85 KB pr. fil i full mode (målt), så 1000 filer ≈ 85 MB. Acceptabelt.

**Sikkerhed:** intet væsentligt fundet. Alle subprocess-kald bruger argumentlister (ingen `shell=True`, `os.system` eller `os.popen`, verificeret med grep). Stier er absolutte, så filnavne med mellemrum virker (harness-stierne har mellemrum). API-nøgler sendes i headere (`bazarr.py:62-63`, `correctness.py:232`), ikke i URL'er, så `requests`-exceptions logger dem ikke. `log.debug("alass: %s", " ".join(cmd))` (`sync_engine.py:130`) er kun kosmetisk.

---

## 3. Profileringsresultat (én række pr. scenarie, SH_S01E01, tiny.en-greedy-cpu, `--fresh-db --redo --audio-confirm off`)

Kommando: `tests/e2e_matrix.py --models tiny.en-greedy-cpu --only SH_S01E01 --scenarios <s> --mode <m>` under `python -m cProfile`. Whisper er erstattet af cachede transskripter i harnessen, så Whisper-tiden står som lydsekunder (`whisper_cost`).

| Række | Wall | Største poster | Whisper-lyd (fresh / cached) |
|---|---|---|---|
| full drift_rand0 (fixed Δ33,2 s) | 3,9 s | alass 1,74 s (1 kørsel, WAV cachet) · collect_samples_full 2× 0,82 s · ffprobe 5× 0,56 s · rate-trin 0,15 s · fps 0,09 s · missing-middle 0,08 s · swap-gate 0,07 s | 0 / 6464 (dobbelttalt, L5) |
| full clean | 1,6 s | collect_samples_full 2× 0,57 s · ffprobe 6× 0,34 s · screen 0,38 s · rate 0,07 s | 0 / 6464 |
| full block_rand0 (SUSPECT, 2 blokke) | 44,4 s | **speech_timeline 39,8 s** (ffmpeg 35,1 s + Silero 4,6 s) · alass 2× 3,0 s · resten ca. 0,6 s | 0 / 6464 |
| sampled clean | 33,9 s | **_vad_says_needs_full 32,7 s** (ffmpeg 26,8 s + Silero 5,0 s + block_witness 0,9 s) · collect_samples 0,42 s · screen 0,32 s | **699 / 450** (gap-probe +249 s) |
| sampled hole_rand0 | 33,4 s | _vad_says_needs_full 32,1 s | 716 / 450 |
| sampled block_rand0 (eskaleret) | 36,2 s | _block_runs_hit → speech_timeline 31,5 s · alass 3,0 s | 3699 / 450 |
| sampled drift_rand0 (eskaleret) | 3,6 s | alass · rate-trin | 3699 / 450 |

Konklusion:
- Ren Python pr. række er ca. 0,6-0,8 s.
- VAD-udtrækningen er 80-97 % af wall-tiden, når den kører.
- Whisper-lyden er den reelle N100-post: 450 s sampled, 700 s med gap-probe og 3700 s ved eskalering. Med tiny greedy ca. 20× realtid på 5800X3D svarer det anslået til ca. 1, 1,5 og 8 min på N100 (3-4× langsommere, ikke målt).
- Rå cProfile-filer og tabeller: `rapport/afsluttende_review_profiler/`.

Testsuiten: 344 passed på 11:13. Varigheder står i tillægget nederst.

---

## 4. Død kode (grep/AST-bevis)

Metode: AST-scanning af alle `Name`/`Attribute`-brug i `verifyarr/` og `tests/`, plus `ruff check --select F401,F841` (kørt via `uvx`, cache fjernet bagefter).

| Symbol | Hvor | Bevis |
|---|---|---|
| `clip_anchor_shift` | `subtitles.py:437` | 0 kald i kode/tests. Importeres ubrugt i `correctness.py:24` og `line_order.py:58`. Resten er docstring-omtaler |
| `check_subtitle` | `line_order.py:868` | 0 kald. Kun nævnt i log-tekster/docstrings |
| `transcribe` | `correctness.py:469` | 0 kald (kun `_extract_and_transcribe`/`transcribe_verbose` bruges) |
| `detect_language_and_transcribe` | `correctness.py:490` | 0 kald |
| `bazarr_current_subtitle_path_movie` | `bazarr.py:246` | 0 kald (kun modul-docstring) |
| `bazarr_history_score` | `bazarr.py:261` | 0 kald (kun modul-docstring) |
| `init_db` | `db.py:439` | 0 hits. Docstringen siger "for verifyarr.cli", men cli bruger den ikke |
| `set_settings_raw` | `db.py:1377` | 0 hits |
| `parse_vad_tsv`, `scan_region_time` | `vad.py:59`, `vad.py:149` | Kun `tests/test_vad.py`. Bevidst beholdt ("offline evaluation and future sidecars", `vad.py:13-14`). Behold, eller brug `parse_vad_tsv` til test-fixtures (M8) |
| Ubrugte importer | `pipeline.py:26` `anchor_points`, `:27` `tilt_from_points`, `:33` `FPS_ANCHOR_TRIM_S`, `:49` `cue_gaps` · `settings.py:353` `db` · `auth.py:15` `os` · `tests/sync_verification.py:46`, `tests/test_sync_verification.py:45`, `tests/test_block_hole.py:8` | ruff F401 |
| `Config.state_db` | `settings.py:135/367` | Sættes, men læses aldrig |

**Ikke død, men overlappende og nu umålt:**
- `_try_fps_rescale`s diskrete gren og `_try_stretch_rescale` (`pipeline.py:1344-1517`) fyrede 0 gange i 732 bk1-rækker. Rate-trinnet (62) og presync (84) har overtaget. Docstringen (`:1383-1387`) forklarer, at stierne uden screen stadig har brug for dem. Mål dem specifikt (bazarr uden conn, screen-exception, screen "unknown"), før de fjernes.
- `anchor_block_clusters` bruges stadig i `_screen_pair` (`pipeline.py:286`) på sparsomme klip-ankre. `anchor_point_runs` arbejder på rå linjepunkter, så de er ikke dubletter, men `anchor_run_offsets` (`correctness.py:936`) og `anchor_point_runs` (`:841`) er samme idé ("k i træk væk fra filens median") og kan samles med parametre.
- `_ramp_rescue_probe` er i brug (16 fyringer i bk1).

---

## 5. Overfitting (tærskler, der ligner SH-tuning frem for princip)

| Konstant | Hvor | Begrundelse |
|---|---|---|
| `POINT_RUN_K=6`, `MIN_DEV=2.0`, `MAX_MAD=0.5` | `correctness.py:836-838` | Kalibreret på tiny + 6 SH ("healthy SH peaks at 1.43s"). Kommentaren siger selv "model-bound". Bekræftet FP på small/turbo uden VAD (K1) |
| `proven_block` k=3/5 s/1,0 | `pipeline.py:1647` | Begrundet med én SH-fil med en gentaget replik |
| `MISSING_MIDDLE_MIN_WORDS_TINY=50` + `"tiny" in model` | `correctness.py:738-743` | "healthy SH peaks at 34". Modelgenkendelse via delstreng er skrøbelig |
| `MISSING_MIDDLE_ESCALATE_GAP_S=50` | `correctness.py:744-750` | Udledt af harnessens egen minimum-hulstørrelse ("60s holes leave >= 56.8s bare"). Cirkulært: tærsklen er tunet til generatoren (`e2e_matrix.py:689`, `lo_s=60`) |
| `RATE_TIGHT_RESID_S=0.30` | `subtitles.py:636` | "healthy SH resid <= 0.28s", dvs. 0,02 s margin. `RATE_TIGHT_TILT_S 0.5` mod SH 0,4, `OFFSET 0.15` mod 0,09 |
| `RATE_MIN_*` | `subtitles.py:622-627` | Kun SH tiny dense pools |
| `ANCHOR_RUN_MIN_DEV_S=1.2` | `correctness.py:929-933` | "three silent half-repaired ones", n=3 |
| `block_witness` (60/30 s, ≥ 6 cues, gain 0,25/0,3, \|s\| ≤ 10) | `vad.py:325-366` | "6/6 silent blocks, 1/6 clean", 12 rækker |
| `RAMP_RESCUE_*` | `pipeline.py:772-777` | "Calibrated on 2 configs (C_S02E04)" |
| `SWAP_GATE_ESCALATE_HEURISTIC=6` | `line_order.py:88-91` | "all 24 swap files (min 7)" |
| `BLOCK_CLUSTER_DEV_S=2.5` | `correctness.py:892-898` | "Healthy SH (tiny) peaks at 1.96s" |

Forslag: held-out-validering på Community-afsnit fra `rapport/testsaet.json` (uden swap). Kør også hver tærskel med én anden model (small.en-q5_1), før den kaldes "kalibreret". Gate alle model-bundne tærskler eksplicit på modellen.

---

## 6. Anbefalet rækkefølge for rettelserne

1. **K1** Uden VAD skal punkt-run-detektoren tie (eller gate på modellen), og compose-modellen skal rettes til tiny.en. Tilføj en matrix-arm uden VAD. *Lille ændring, fjerner en bekræftet FP i standardopsætningen.*
2. **K2** Gem `full_coverage`/`fps_points` i cachen og tilføj en skemaversion til `cache_key_for`. Test: dobbeltkørsel giver samme dom.
3. **H2** `_strip_private(row)` før persistering og rapport. Test: generate + direkte alass-skrivning.
4. **H1 + M6** Genbrug alass-WAV'en til VAD, persistér VAD-tidslinjen i DB, memoisér ikke `None`, og slet WAV'en pr. video efter correctness. *Største tidsgevinst: ca. 30 s pr. fil.*
5. **H4** Timeout skaleret med filstørrelse.
6. **H3** Proceslås mellem CLI og webjob.
7. **M4** Dom-kæde: detektorer efter resync, nulstil blokfelter ved rate-rettelse, `_block_repair_parts` fra strukturerede felter, indholdstjek i rate-trinnet. Gør det sammen med **M9**-opdelingen (verdict-chain som liste).
8. **M2 + M5** Per-fil-kontekst til filtreret transskript, varighed og sprog (fjerner dobbeltlæsninger, WARNING-spam og det manglende loop-filter).
9. **M1** Cache og loft på gap-proben, og brug VAD til at skippe tavse huller.
10. **M7** Jitter-dom før resync-skrivning.
11. **M3** Tyndet Theil-Sen (kræver en matrix-validering).
12. **M8** Testhullerne (inkl. fixture-VAD, så de rigtige-data-tests bliver hurtige), og derefter **L1-L11** oprydning (død kode, forældede kommentarer, README).

Efter hvert trin: `pytest` (grøn) + FP-kontrol på SH (facit) + detektionsscenarierne. Kør fuld matrix først efter trin 2 og trin 7.

---

## Tillæg: kørsler foretaget under reviewet

- `tests/e2e_matrix.py` enkeltrækker (8 stk., cProfile) med `--fresh-db --redo`. Bemærk: `--redo` slettede de delte `tests/e2e_work_matrix/e2e_matrix_tiny_en-greedy-cpu_{full,sampled}.db` (regenererbare caches). Mine `tests/review_prof*.jsonl` er fjernet igen.
- `rerun_cache.py` (K2) og `novad_fp.py` (K1) kørte i egne temp-mapper under scratchpad, ikke i repoet.
- `uvx ruff check` (read-only; `.ruff_cache` slettet).
- Fuld pytest (`--durations=40`, `-p no:cacheprovider`): **344 passed, 29 subtests, 673,7 s (11:13), exit 0.** Grøn.

**Testvarigheder (de 10 langsomste):**

| Test | Varighed |
|---|---|
| test_screen_order::test_block_errors_get_no_pre_sync | 177,4 s |
| test_silent_rows::test_resync_remainder_warns | 43,0 s |
| test_drift_ramp::test_step_file_still_keeps_blocks | 39,5 s |
| test_sh04::test_sh_s01e04_full_untouched_and_unflagged | 33,1 s |
| test_fps_rescale::test_healthy_file_untouched | 32,7 s |
| test_screen_order (5 tests) | 27-31 s hver |

- Tilsammen ca. 510 s af de 674 s.
- Mønsteret (ca. 27-35 s pr. ny video) passer med én fuld lydudtrækning pr. kørsel. `test_screen_order._run` (`tests/test_screen_order.py:37-72`) kalder `process_pair` uden `audio_cache`, så alass ekstraherer hver gang: 3 seeds × (alass-udtrækning + VAD) = 177 s.
- Harnessen har allerede færdige staging-WAV'er (`e2e_matrix.audio_cache_for`, `tests/e2e_matrix.py:252-263`), men dels bruges de ikke her, dels ignoreres de altid af VAD (H1).
- Forslag: send `M.audio_cache_for(slug, video)` med i `_run`, og ret H1. Anslået spares 5-7 min af suiten.

**Test-tempfiler lækker:**
- 10 testfiler bruger `tempfile.mkdtemp(...)` uden oprydning (`test_screen_order.py:45`, `test_drift_ramp.py:52`, `test_fix_61_62.py:55`, `test_fps_rescale.py:48/78`, `test_fps_guard.py:43`, `test_swap_gate.py:63`, `test_sh04.py:43`, `test_cache_model_key.py:45/135`, `test_sync_verification.py:670`).
- Målt nu: **2592 mapper, 3,7 GB i `/tmp`** (heraf 654 `screen_*`).
- Forslag: `TemporaryDirectory` + `addCleanup`.
