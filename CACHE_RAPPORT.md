# CACHE_RAPPORT: model-nøgle, atomar WAV, tærskel 0,25 s

Dato: 2026-09-22. Kode under test: HEAD `5e3fc58` + arbejdstræet.
Model: `tiny.en-greedy-cpu`, frisk DB pr. række, `--workers 14`.
Før = `tests/fix2_final*.jsonl` (HEAD-adfærd, 1656/1656 ok).

## 1. Dommen, pr. ændring mod den rigtige bar

| Ændring | Arm 1 (ret 100 %) | Arm 2 (det 100 %) | Raske filer |
|---|---|---|---|
| Fund 1+2 (model-nøgle + atomar WAV) | 0 celler af 1656 | 0 celler | 0 |
| Fund 3 (tærskel 0,50→0,25) | 96,4 % rettet, 6 stille — **identisk, samme 6 celler** | 88,8→**91,2 %** detekteret (38→30 stille), +8 fuldt rettede | clean 68/72 identisk; genuine 1 ny omskrivning (evidensbåret, se §5) |

Fund 1+2 er adfærdsneutrale som forudsagt (én model, gyldige WAVs):
`cache12` vs `fix2_final` giver **0 ændrede celler**, identisk Whisper-omkostning
og identisk note-hash. Fund 3 flytter **112 celler**, alle mekanisk forklaret
nedenfor. Intet forbedrer arm 2 på bekostning af arm 1. Suiten er grøn
(228 passed, +16 nye). **Ikke committet.**

## 2. Fund 1: model i klip-cache-nøglen

**Designvalg: sameksistens, ikke overskrivning.** Nøglen er udvidet fra
(video, slot) til (video, slot, STT provider, STT model) — to modellers rækker
ligger side om side pr. slot. En læsning under fremmed model er et rent miss,
aldrig en sletning; en skrivning under B rører ikke A's række. Det opfylder
begge krav bogstaveligt (A serveres aldrig som B; ingen slette-og-hente-igen-
løkke ved skift frem og tilbage) — hvor nabotabellens overskrivningsmønster
ville koste en re-transskribering pr. skift, præcis det opgaven frabeder sig.
Prisen er en engangs-PK-rebuild ved migrering (SQLite kan ikke ændre PK in
place) og lidt flere tekstrækker på disken; rækkerne ældes ud på 30 dage som før.

- Nøglen kommer fra den ene funktion `full_transcript_cache_key`, som er flyttet
  fra `correctness.py` til `db.py` så også `vad.py` kan nøgle sine læsninger
  uden circulært import (`correctness` re-eksporterer; alle eksisterende
  referencer virker uændret). Fallback-model-caveat'en deles med fuld-cachen:
  nøglen er den *konfigurerede* model, ikke nødvendigvis den der faktisk
  transskriberede ved fallback.
- NULL-konventionen er fulgt: rækker uden nøgle (gemt før kolonnerne fandtes)
  accepteres under enhver model; deler én slot med en nøglet række, vinder den
  nøglede. mtime/size-mismatch sletter fortsat ALLE videoens rækker (lyden er
  væk — gælder alle modeller).
- Kaldere med nøgle nu: `correctness_check` (læs+skriv),
  `evaluate_against_cached_transcripts` (ankre!), `collect_samples` (3 læs,
  1 skriv), `timeline_for_video`. Forældede "keyed on"-kommentarer i
  `db.py`/`correctness.py`/`pipeline.py`/`e2e_matrix.py` er opdateret.

**Migrering (krav 2):** `test_old_db_migrates_without_losing_rows` bygger en
gammel-skema-DB (frosset HEAD-DDL + 2 rækker), åbner den med `connect()` og
viser at begge rækker overlever med indhold, er læsbare via den nye API og kan
stå ved siden af nye nøglede skrivninger. Rebuild'en er no-op på friske DBs
(PK-tjek via PRAGMA, eksplicit kolonneliste da ALTER lægger nye kolonner sidst).

**Tests/mutation:** 6 db-tests, alle grønne efter, alle 6 røde med rettelsen
stash'et (sammen med de 10 øvrige nye: 16/16 røde uden rettelsen).

## 3. Fund 2: atomar `extract_audio_wav`

ffmpeg skriver nu til `<navn>.part` ved siden af, og først efter
exit-0 **og** bestået `wav_complete` (RIFF/WAVE-magi + erklæret størrelse =
faktisk filstørrelse) flyttes filen på plads med `os.replace`. Fehl tessletter
tmp'en og rører aldrig en tidligere god fil (`-y` trunkerede den før).
Harnesset (`audio_cache_for`) afviser nu en ugyldig staging-WAV med samme
validator i stedet for at seede den blindt — alle 60 staging-WAVs validerer,
så målingerne er urørte af den ændring. Som opgaven: ingen live fejl fundet
her; hullerne var reelle, men umødte.

**Tests/mutation:** 8 tests (4 validator-enheder + 4 mock'ede ffmpeg-forløb:
fejl/timeout/afkortet-ved-exit-0/succes), alle røde uden rettelsen — de tre
adfærdsafgørende fejler på assertion (delvis fil efterladt / afkortet WAV
accepteret), ikke på TypeError. `test_seed_points_at_existing_wav` skriver nu
en minimal gyldig WAV i stedet for `b"fake-wav"` (forventningen er uændret).

## 4. Fund 3: tærskel 0,50→0,25 s, begge steder

`SCREEN_TOLERANCE_S` og `sync.min_change_seconds`-defaulten er begge 0,25 med
opdaterede kommentarer; `test_screen_and_write_gate_agree` pinder at de to
steder er enige. **Opgraderings-semantik (som opgaven beder om):** defaulten
rammer kun installationer uden gemt værdi (`get_all_settings`: gemt række
vinder). En opgraderer der nogensinde har gemt sync-indstillinger **bliver på
0,5 i skrivegaten**, mens screenen (kodekonstant) flytter for alle — altså
0,25-screen + 0,5-gate: flere alass-kørsler, samme skrivegrænse. Friske
installationer får 0,25/0,25. Matricen herunder målte 0,25/0,25 (frisk DB
følger defaulten; `cfg_for` pinder den ikke). Ingen migrering af gemte 0,5'er
er lavet — det er brugerens beslutning.

**112 ændrede celler** (`cache_all` vs `cache12`), 16 verdict-skift + 96
sti-skift med samme verdict:

- **8× advaret→rettet, alle cut_version full (SH):** rec 0,44-0,51→0,97-1,00 via
  alass 2-blok. 0,5-screenen havde clearet dem ("already in sync" på en fil med
  300 s-hop); 0,25 sender dem til alass. Rena gevinster, fortsat SUSPECT-flagget.
- **8× stille→advaret:** C_S02E12 cut_version/piecewise_c sampled (rec ~uændret,
  nu ærligt flagget) samt C_S02E02/C_S03E10 cut_version sampled, hvor den
  nyskrevne single-offset-fix er **dårligere end ingenting** (rec 0,58→0,05 og
  0,56→0,00) — men flagget. Mekanismen er en eksponeret, præ-eksisterende
  svaghed i verify-stien (den skriver en SUSPECT-scoret kandidat: "kept 'new'
  [score 0.10 (SUSPECT)..."), ikke tærskellogik i sig selv. Uden for scope;
  noteret her så beslutningen er informeret.
- **34× uniform_p03 already→fixed (Δ0,3-0,4):** den tilsigtede grænseflytning.
  6 var allerede fixed ved 0,5; 32 står fortsat urørte (målt < 0,25 — støjgulvet).
- **14× uniform_m07:** 6 nye reelle fixes (2× Δ0,7; 4× Δ0,5 på C_S03E16 —
  restfejl ~0,2 s), 8× Δ-vrik (1,0/1,1→0,9), rec 1,0 overalt.
- **10+6+4+4 × uniform/p15/m5/neg:** Δ-vrik på 0,1 s (rec 1,0). uniform_neg
  full (6.1-cellerne!) når nu via verify-sti i stedet for presync (spread
  0,21/0,25 ≥ 0,25 nedlægger veto) — samme filresultat, anden vej.
- **4× dropdup already→fixed (Δ0,3s, rec 1,0):** støjgulv-skrivning på
  timing-sunde-men-skade
...[truncated 2336 chars]