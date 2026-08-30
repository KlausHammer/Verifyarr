# verifyarr

Self-hosted subtitle sync + verification for a Plex/Bazarr library.

1. **Syncs** every subtitle against its own video with [alass](https://github.com/kaegi/alass),
   which finds multiple sync points per file (handles mid-episode jumps, not just a global
   offset). `.srt`/`.ass`/`.ssa`/`.vtt`.
2. **Checks the subtitle is actually right** — samples a few audio clips, transcribes them with
   Whisper (Groq or OpenRouter), and compares the words against the subtitle at those timestamps.
3. **Cleans up suspect files on its own**, if you turn it on: quarantine (never permanent
   deletion), tell Bazarr to blacklist the source, or have it fetch a replacement itself.


## Setup

Grab the compose file and edit its `volumes:` to point at your real media folders (it builds
straight from this repo, no separate clone needed):

```bash
curl -O https://raw.githubusercontent.com/KlausHammer/Verifyarr/main/docker-compose.yml
docker compose up -d
```

Open `http://your-server:8787`, create an admin password, then go through Settings: General
(Root Folders), Correctness (a free [Groq](https://console.groq.com/keys) API key), Bazarr
(URL + API key), Automation, Scheduling.

Forgot the admin password? `docker exec -it verifyarr python3 verifyarr.py reset-password`.


## Connecting it to Bazarr

Settings → General → Post-processing → **Use post processing**, command for series:

```
docker exec verifyarr python3 verifyarr.py single \
  --video "{{episode}}" --subtitle "{{subtitles}}" --lang "{{subtitles_language_code2}}" \
  --provider "{{provider}}" --subs-id "{{subtitle_id}}" \
  --series-id "{{series_id}}" --episode-id "{{episode_id}}"
```

(For movies, swap in Bazarr's movie placeholders — `{{movie}}`, `{{radarr_id}}`, etc. — check
Bazarr's own placeholder list.) Needs the Bazarr container to be able to `docker exec` into this
one. If that's not set up, the periodic sweep (Settings → Scheduling) catches new downloads too,
just on a delay.


## Action on a suspect file

Set per-check (correctness / line-order) under Settings → Automation:

| Value | Does |
|---|---|
| `off` *(default)* | Flags it in the report, nothing else |
| `quarantine` | Moves it to `/data/quarantine` |
| `blacklist` | + tells Bazarr to blacklist that source |
| `remediate` | + tries to fetch a working replacement via Bazarr, testing each candidate itself |

Nothing is ever permanently deleted — quarantined/backed-up files sit under `/data`, restorable
from the webapp's Quarantine page.


## Notes

- Everything is configured in the webapp, not env vars — `docker-compose.yml` only needs real
  Docker settings (volumes/port/TZ).
- The correctness check skips audio in a language Whisper isn't reliable for by default
  (configurable); a subtitle in a different language than the audio is machine-translated before
  comparing, so language alone never causes a false flag.
- Only one job (sweep/single) runs at a time; cancelling one stops it between files, not mid
  API call.
- Movies are supported for sync/correctness; `blacklist`/`remediate` are series-only for now.
