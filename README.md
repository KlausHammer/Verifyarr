# verifyarr

A self-hosted tool for your Plex/Bazarr library that does three things:

1. **Syncs** every subtitle file against its own video with [alass](https://github.com/kaegi/alass) —
   which (unlike Bazarr's built-in ffsubsync) can find *multiple* sync points in the same file.
   Fixes both slow drift (e.g. wrong framerate) and sudden jumps mid-episode (e.g. where your
   version cut a scene the subtitle still accounts for). Supports `.srt`, `.ass`, `.ssa`, `.vtt`.
2. **Sample-checks whether the subtitle is even the right one**, by taking a few short audio
   clips spread across the episode, transcribing them with Whisper (via Groq or OpenRouter), and
   comparing the words against the subtitle at the same timestamps.
3. **Cleans up on its own** when a file is suspect — moves it to a quarantine folder (never
   permanent deletion) and can optionally tell Bazarr to blacklist that specific source via its
   API, so it doesn't just get downloaded again.

The sync step always runs automatically without confirmation (it backs up the original to
`/data/backups` first). The correctness check is inherently a heuristic — but that doesn't mean
manually reviewing every file: see **"Automatic action"** below for how it can run safely on its
own, without ever deleting anything permanently.

**The default is conservative, though:** every automatic action is `off` by default — files are
only flagged in the report/webapp until you turn automation on yourself. See "Test it safely
first" below.


## Webapp

All control and configuration happens through a built-in webapp (styled like Bazarr/Sonarr) on
port `8787` — `http://your-server:8787`. There is **nothing** to edit in `docker-compose.yml`
beyond your own media folders and port/TZ (see "Docker Compose setup" below); everything else —
Root Folders, Groq/OpenRouter/Bazarr keys, thresholds, automation, scheduling, admin password —
is set and saved from the page itself, in `/data/verifyarr.db`.

The first visit shows a setup screen where you create an admin password (there is no default
password). Forgot it later? Run
`docker exec -it verifyarr python3 verifyarr.py reset-password` — that only resets the login,
not your other settings.

Pages:

| Page | What it shows |
|---|---|
| **Movies / Series** | Grouped per movie/series (series can be expanded per season), with Scan/Rescan for the whole page, per title, and (series) per season |
| **Files** | Every file processed so far, including ones missing a subtitle entirely — searchable/filterable, with "run now"/"remediate" buttons individually or on selected files |
| **Activity** | History of runs (sweep/single, manual/scheduled/CLI/Bazarr hook), with a live log and a cancel button for a running job |
| **Stats** | The home page — status overview, match rate over time, score distribution, language/movie-vs-series breakdown, and "run sweep now" |
| **Quarantine** | Browser for the quarantine and backup folders, with a restore button (see "Undoing something") |
| **Bazarr blacklist** | Log of what verifyarr itself has had Bazarr blacklist |
| **Settings** | All settings, split into tabs (General/Sync/Correctness/Automation/Bazarr/Scheduling/Log/Account) |

The CLI (`sweep`/`single`) and Bazarr's post-processing hook keep working unchanged alongside
the webapp — they share the same database, so a run triggered from the terminal shows up in
Activity just like one triggered from the UI.


## Test it safely first

Before pointing it at the whole library or turning automation on, you can fully scope it down
from Settings in the webapp:

1. **Run on just one folder** — under Settings → General, set Root Folders to a single
   series/folder's path instead of the whole library. The tool only ever touches files under the
   folders you set there.
2. **Dry run** — under Settings → Automation, turn on "Dry run" and run a sweep. You get the full
   report (what it would fix, what it would flag) in Library/Activity without anything on disk
   actually changing. This is the safest first step.
3. Once the report looks right: turn dry run off and let it fix sync for real on the same scoped
   folder — the originals still live in `/data/backups`, so it's easy to undo (see the Quarantine
   page).
4. Each check's "Action if SUSPECT" (Settings → Automation) stays `off` until you change it
   yourself — so even once sync runs for real, nothing happens automatically to a suspect file
   until you actively turn on `quarantine`/`blacklist`/`remediate` (see "Automatic action" below).
5. Expand gradually: more Root Folders, and eventually the whole library, at whatever pace feels
   safe.


## About language handling, and why Whisper doesn't just fail on non-English audio

Groq's/OpenRouter's transcription endpoint is multilingual and transcribes in the source
language when told which language is being spoken — only a *separate* translation endpoint
always forces English output, and that's not what this tool uses for transcription.

Still, Whisper is noticeably better at English than most other languages (like most ASR models —
there's just more English training data), so the tool is built like this:

- It figures out what language is **spoken** in the video (from the file's own language metadata
  via ffprobe, or — if that's missing — a quick Whisper guess on the first sample).
- The "require audio language" setting (default `en`) decides whether the check runs at all. If
  the speech is a different language and doesn't match, the file is skipped entirely — no Whisper
  calls, no risk of false flags on audio Whisper isn't reliable for. Set it empty to run the
  check regardless of spoken language (less reliable for non-English audio, but better than
  nothing).
- The subtitle's **own** language can easily differ from the audio — that's the normal case for
  a translated subtitle on a foreign series. If the audio is English (and so transcribed in
  English) but the subtitle isn't, the short subtitle excerpt is automatically translated to
  English (via a small, cheap language model) before comparing — so the language difference alone
  never triggers a false SUSPECT flag. If the subtitle is also English, it's compared directly
  with no extra step.

In short: a foreign-language subtitle on an English-audio show **does** get checked (against the
English audio, via translation), while audio in an unsupported language is skipped by default,
because Whisper isn't reliable enough there to be the judge.

The default transcription model is the full `whisper-large-v3` (not turbo), because turbo cuts
the decoder layers from 32 to 4 for speed — which typically hurts non-English audio the most. The
price difference is negligible at the scale this runs at (see "Costs").


## Where it looks for subtitles

1. Same folder as the video, with a filename starting with the video's name — Bazarr's normal
   layout.
2. If nothing's found there, it looks one folder down (e.g. a `Subs/` or `Subtitles/`
   subfolder) and matches either on the filename starting with the video's name, both sharing the
   same `SxxEyy` pattern (for series), or — for movies — the video's folder containing only one
   video at all (so there's no ambiguity about which video a subtitle in a subfolder belongs to).

The language code is read from the filename (e.g. `.en.srt`, `.en.forced.srt`) wherever the file
lives.


## Automatic action on suspect files

Controlled per-check by "Action if SUSPECT" on Settings → Automation — correctness and line-order
checks each have their own independent action:

| Value | What happens |
|---|---|
| `off` *(default)* | Flag in the report only — nothing is touched |
| `quarantine` | Moves the file to `/data/quarantine` (same folder structure as the library) — Plex stops showing the wrong subtitle, but Bazarr doesn't know and may download the exact same file again |
| `blacklist` | Everything `quarantine` does, **plus** an API call to Bazarr that blacklists that specific source (provider + subtitle id), so Bazarr won't fetch the same wrong file again. Requires a Bazarr URL + API key |
| `remediate` | Everything `blacklist` does, **plus** tries to fetch a working replacement itself — see **"Fetching a replacement automatically"** below. Series/episodes only for now, not movies |

Nothing is ever permanently deleted — the quarantine folder is the same "undo" mechanism as the
backup folder used for sync fixes, see **"Undoing something"** below. Blacklisting in Bazarr is
also fully reversible (just remove it again under Bazarr's own Blacklist tool in its UI).

**How it decides what to blacklist:**

- On a **single** call (right after a fresh download, see the Bazarr integration section below),
  Bazarr itself sends the provider/subtitle-id/series-id/episode-id as arguments — no lookup
  needed.
- On a **sweep** of existing files on disk, those ids aren't known upfront, so the tool fetches
  Bazarr's download history (`/api/episodes/history` and `/api/movies/history`) once per sweep
  and looks up the file's path there. If there's no history for the file (e.g. a subtitle you
  added manually), it's still quarantined, just not blacklisted — that shows up in the report's
  `auto_action` field.
- **Paths have to match.** The history lookup only works if Bazarr and verifyarr see the same
  file under (optionally translatable) paths. If they run on the same Docker network with
  different mount points for the same library, fill in the path mapping under Settings → Bazarr,
  e.g. `/media/tv=/tv` if Bazarr sees your series under `/tv` while this container sees them
  under `/media/tv`.

If unsure it'll hold up in practice, start with `quarantine` (no Bazarr calls, no requirement for
paths to match), watch the Library page for a few days, and switch to `blacklist` once
comfortable. The "Test connection" button under Settings → Bazarr confirms the URL+key work
before turning anything on.


## Fetching a replacement automatically (`remediate`)

Instead of just blacklisting and leaving the language missing, `remediate` tries to fetch a
working replacement itself, in this order:

1. **Ask Bazarr to search and fetch on its own** — same action as the search icon in its UI.
   Respects Bazarr's own minimum-score setting (Settings → Subtitles), so it only finds something
   if a candidate actually clears that threshold.
2. If a new file appears, **it's tested with the same alass+Whisper check** as the rest of the
   tool. If it passes, done — nothing more happens.
3. If it fails, **that one is blacklisted too**, and up to `REMEDIATE_MAX_ATTEMPTS` (default 3)
   more attempts are made with manually picked candidates from Bazarr's full provider search —
   sorted by Bazarr's score, but **ignoring its minimum-score setting**. The point is to test
   candidates Bazarr itself would reject as too uncertain, using this tool's own Whisper check as
   the judge instead of Bazarr's name-/release-based score.
4. If none of that works, the language is left **missing** — the same state as if Bazarr had
   never found anything. Bazarr's own periodic search will try again later, and the attempt is
   logged in the report's `auto_action` field, so it's visible that nothing suitable was found
   rather than just assuming so.

Like `blacklist`, this requires Bazarr metadata (from the `single` arguments, or a history lookup
during a sweep, see above) — without it, it falls back to plain `blacklist`. Only series/episodes
are supported for now (same limitation as the rest of the Bazarr integration).


## Requirements

- Docker
- Access to your media folders (the same ones Plex/Bazarr use)
- A free [Groq](https://console.groq.com/keys) API key for the correctness check (can be turned
  off under Settings → Correctness if you only want the sync part) — OpenRouter is also supported
  as an alternative provider
- Bazarr's API key (Settings → General → Security), only needed for the `blacklist`/`remediate`
  automation levels


## Docker Compose setup

1. Put this whole folder somewhere on your server, e.g. `/opt/verifyarr/` (or import it as a
   stack in a tool like Dockge/Portainer).
2. Open `docker-compose.yml` and edit only `volumes:` — the left side of each line should point
   at your real media paths (the same ones Plex/Bazarr already use), the right side is the path
   *inside* the container that you'll pick as a Root Folder in the webapp afterward. Adjust
   `ports:` and `TZ` too if needed. Nothing else in this file needs touching — keys and the rest
   of the configuration are set after startup.
3. Build and start it: `docker compose up -d --build` (first build takes a couple of minutes,
   since alass is compiled from source and the frontend is built in a build stage).
4. Open `http://your-server:8787` — the setup screen asks you to create an admin password.
5. Go through the Settings tabs: General (Root Folders — must match the right side of your
   `volumes:` lines), Correctness (API key), Bazarr (URL+key, "Test connection"), Automation and
   Scheduling. Everything saves immediately, no restart needed.

To see it work right away instead of waiting for the schedule, click **"Run sweep now"** on
Stats or Activity — or run a one-off sweep from the terminal:

```bash
docker exec -it verifyarr python3 verifyarr.py sweep --force
```

(`--force` ignores what's already been processed and reprocesses everything — normally run it
without `--force`, so it skips files it's already handled and that haven't changed since. Runs
triggered this way show up on the webapp's Activity page like anything else.)


## Periodic sweep of the whole library

Runs automatically in the background of the webapp process itself (no cron), controlled by
Settings → Scheduling → cron expression (default: Sunday at 04:00 UTC — the container's `TZ`
only affects log timestamps, the schedule itself is computed in UTC). Changes take effect
immediately, no restart needed.

It tracks what it's already processed in `/data/verifyarr.db` (the `files` table, visible on the
Files page), so it only looks at new or changed files on the next run — the first run over a
whole library can take a while (both alass and any Whisper calls per file), but after that it's
just the delta. Each run's progress and log are visible live under Activity.


## Connecting it to Bazarr (new downloads get checked immediately too)

Bazarr has a "custom post-processing" feature that runs a command every time it fetches/touches
a subtitle.

1. In Bazarr: **Settings → General → Post-processing**, turn on **Use post processing**.
2. Enter as the command (adjust the path if the container lives elsewhere). For **series**:

   ```
   docker exec verifyarr python3 verifyarr.py single \
     --video "{{episode}}" --subtitle "{{subtitles}}" --lang "{{subtitles_language_code2}}" \
     --provider "{{provider}}" --subs-id "{{subtitle_id}}" \
     --series-id "{{series_id}}" --episode-id "{{episode_id}}"
   ```

   For **movies**, the exact placeholder names Bazarr uses can vary slightly by version from the
   series ones — Bazarr's own Settings page lists the available variables once post-processing is
   turned on. Find the movie variants of `{{provider}}`, `{{subtitle_id}}`, and a movie id
   (typically something like `{{radarr_id}}` or `{{movie_id}}`) in that list, and use them here:

   ```
   docker exec verifyarr python3 verifyarr.py single \
     --video "{{movie}}" --subtitle "{{subtitles}}" --lang "{{subtitles_language_code2}}" \
     --provider "{{provider}}" --subs-id "{{subtitle_id}}" --radarr-id "{{YOUR_MOVIE_ID_PLACEHOLDER}}"
   ```

   This command requires the Bazarr container to be able to run `docker exec` against verifyarr —
   which usually means Bazarr needs access to the Docker socket. If that's not desirable, the
   simplest alternative is to rely on the periodic sweep instead (set the cron expression under
   Settings → Scheduling to e.g. `0 * * * *` for hourly) — new downloads just get caught on the
   next run, with a bit of delay, and via a Bazarr history lookup instead of direct arguments for
   blacklisting.


## Understanding the report

Each run writes one line per file to `/data/reports/report-<timestamp>.jsonl` (one JSON line per
file, easy to `grep`/`jq`). Key fields:

| Field | Meaning |
|---|---|
| `sync_status` | `already in sync`, `fixed (Δx.xs)`, `alass not found`, or an error message |
| `sync_max_shift_s` | How much the biggest line moved, in seconds |
| `structural_change` | `true` if the number of subtitle lines changed significantly — worth checking manually |
| `sync_split_blocks` | Number of separate sync blocks alass found necessary. 1 = one even shift (typical for e.g. wrong framerate). Multiple with inconsistent shifts *can* mean a wrong subtitle (forced local fitting) — but can also just be real cuts/scene changes, see `note` |
| `correctness_flag` | `ok`, `SUSPECT`, `disabled`, or `skipped` (e.g. unsupported spoken language) |
| `correctness_avg_score` | 0.0–1.0, average share of what Whisper heard that's also found in the subtitle at the same timestamp. The flag is decided by whether the *majority* of individual samples clear the threshold on their own, not just the average |
| `auto_action` | What was done for a suspect file: quarantine path and any blacklist status |
| `note` | For suspect files: an excerpt of what Whisper actually heard, so you can judge for yourself |

The log at the end of each run also summarizes the number of fixed/failed/suspect files.


## Undoing something

Easiest via the webapp's **Quarantine** page (both quarantine and backups, with a "Restore"
button per file). Manually via the filesystem still works the same as before:

- **A sync fix:** the original lives in `/data/backups/`, in the same folder structure as the
  library, named `<original-name>.<timestamp>.orig.<ext>`. Just copy it back.
- **A quarantined/blacklisted file:** the file sits untouched in `/data/quarantine/`, named
  `<original-name>.<timestamp>.<ext>` — move it back to its original location. If it was also
  blacklisted in Bazarr, remove it from the blacklist under Bazarr's own Blacklist tool (the
  Tools page for the series/movie), or see it under the webapp's **Bazarr blacklist** page.


## Configuration

Everything is set in the webapp under **Settings**, stored in `/data/verifyarr.db` — there are no
environment variables to edit in `docker-compose.yml` (only `TZ`, `ports`, and your `volumes`,
which are real Docker-level requirements). The tabs map to these groups:

| Tab | Settings |
|---|---|
| **General** | Root Folders (folders scanned — picked among your `volumes:` mounts), subtitle languages, video file types |
| **Sync** | split-penalty, minimum change to count as a fix, Whisper sampling parameters, line-order check |
| **Correctness** | on/off, provider + API key, transcription/translation model, sample count, clip length, comparison window, overlap threshold, required audio language |
| **Automation** | "What runs" (per-trigger toggles + per-check "Action if SUSPECT"), max remediation attempts, dry run |
| **Bazarr** | URL, API key, path mapping, "Test connection" button |
| **Scheduling** | Cron expression for the periodic sweep (UTC), run-on-start, Bazarr wanted-subtitles poll, library folder-watch poll |
| **Log** | Log level, live log viewer |
| **Account** | Change admin password |

**For upgrades from the old env-var-only version:** if a `.env`/environment variables were
already set in the compose file (`GROQ_API_KEY`, `BAZARR_URL`, etc.), they're automatically
imported into `/data/verifyarr.db` on the very first startup after upgrading — no need to
re-enter them, but remove them from `docker-compose.yml` afterward (they're ignored post-import,
but clutter). The old `/data/state.db` (the previous, thin processing log) is **not** imported —
the first sweep after upgrading therefore reprocesses every file, as if `--force` was used, and
builds the new `files` table up from scratch.


## Costs

With default settings (3 samples × 30 sec. per file), a library of a few hundred episodes uses a
couple hours of audio total. Groq's free tier covers 2,000 calls/day, so even a large library can
be checked for free spread over a few days initially; later sweeps only hit new/changed files and
are effectively free ongoing. The few translation calls (only on a language mismatch between
audio and subtitle) use a small, cheap model — a few hundred tokens per file, still negligible.
On paid usage, `whisper-large-v3` costs $0.111 per hour of audio — still a few cents for a whole
library.

**Hitting a rate limit** (e.g. tokens-per-minute on the free tier — especially relevant during a
large sweep with many translation calls) makes the tool wait automatically until the limit clears
(using the provider's own `Retry-After` header) and retry — it doesn't fail or skip the file, it
just takes longer. This applies to every API call (transcription and translation).


## Known limitations

- Subtitle format: `.srt`, `.ass`, `.ssa`, `.vtt` are parsed/compared (via `pysubs2`). Pure
  bitmap formats (VobSub `.sub`/`.idx`, PGS) aren't supported by the comparison/parsing logic.
- The correctness check requires the spoken language to be known or guessable by Whisper — if
  both metadata and the guess are unreliable or missing, the file is skipped rather than guessed
  wrong.
- Cross-language comparison (e.g. English audio + a translated subtitle) is only fully supported
  when the audio matches the required-audio-language setting (default English). Setting it empty
  to run regardless of spoken language falls back to a less reliable direct comparison for other
  combinations.
- Automatic blacklisting of already-existing files (sweep mode) requires Bazarr and verifyarr to
  see file paths the same way (optionally via path mapping), and the file to exist in Bazarr's
  download history. Subtitles added manually are still quarantined, just not blacklisted.
- One sweep runs the whole library in a single background thread at a time (only one job can run
  at once); if the library is very large, schedule the sweep less often instead.
- Cancelling a running sweep (Activity page) is cooperative — it stops between files, not mid an
  ongoing alass or API request, so it can take a few seconds from click to actually stopping.
