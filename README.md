# verifyarr

Self-hosted subtitle sync + verification for a Plex/Bazarr library.

1. **Syncs** every subtitle against its own video with [alass](https://github.com/kaegi/alass),
   which finds multiple sync points per file (handles mid-episode jumps, not just a global
   offset). `.srt`/`.ass`/`.ssa`/`.vtt`.
2. **Checks the subtitle is actually right** — samples a few audio clips, transcribes them with
   Whisper (Groq or OpenRouter), and compares the words against the subtitle at those timestamps.
3. **Cleans up suspect files on its own**, if you turn it on: quarantine (never permanent
   deletion), tell Bazarr to blacklist the source, or have it fetch a replacement itself.
4. **Generates a subtitle from scratch**, if you turn it on, for a video that has none at all —
   full-track Whisper transcription (Groq, OpenRouter, or Cloudflare Workers AI), translated with
   an LLM (Groq, OpenRouter, or Gemini) into any wanted language Whisper didn't already speak.


## Setup

Grab the compose file and edit its `volumes:` to point at your real media folders (it builds
straight from this repo, no separate clone needed):

```bash
curl -O https://raw.githubusercontent.com/KlausHammer/Verifyarr/main/docker-compose.yml
docker compose up -d
```

Open `http://your-server:8787`, create an admin password, then go through Settings: General
(Root Folders), Correctness (a free [Groq](https://console.groq.com/keys) API key), Bazarr
(URL + API key), Automation, Scheduling. Generate (see below) is optional and off by default.

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
| `blacklist` | Tells Bazarr to blacklist that source, which removes the file and makes Bazarr search for a replacement on its own |
| `remediate` | Same as `blacklist`, then waits for and tests whatever Bazarr finds itself; if that fails, tries more candidates from Bazarr's provider search until one passes or attempts run out |

`blacklist`/`remediate` hand the file to Bazarr rather than quarantining it locally, since Bazarr
only auto-searches for a replacement once it's actually deleted the file. Nothing is lost even
so: if "Back up subtitles" is on (Settings → General), a copy is saved to `/data/backups` first.
If Bazarr can't be reached or has no record of the file, it falls back to a local quarantine
move instead.


## Generating missing subtitles

Off by default (Settings → Generate → turn it on, plus its own switch under Automation → What
runs). For a video with no subtitle at all in a wanted language:

1. The whole audio track is transcribed with Whisper, in chunks, with real timestamps — not the
   short sampled clips the correctness check uses. Chunks are cut in silences rather than at
   fixed offsets, long silences aren't uploaded at all, and segments Whisper itself reports low
   confidence in are dropped (that's what a hallucinated caption over music looks like).
2. If the wanted language isn't the language actually spoken, the already-timed lines are
   translated with an LLM (never re-timed — only the text changes). If any line fails to
   translate, no file is written at all, rather than one with untranslated lines left in it.
3. The result is written as `<video>.<lang>.srt` next to the video and synced with alass.

The Whisper correctness check is deliberately **not** run against a generated subtitle — it
compares a subtitle to a Whisper transcript of the same audio, which is where this file came
from, so it can only ever confirm itself. The checks in steps 1 and 2 take its place. A generated
file shows up as `generated` rather than as a check that passed.

One caveat worth knowing before turning this on: once the file exists, Bazarr considers that
language covered and stops looking for a real subtitle for it.

Uses its **own** API keys and provider choice (Settings → Generate), separate from the
correctness check's — a free tier's quota for a full-length transcription job is easy to exhaust,
so `Max. videos per day` caps how many distinct videos get generated in any 24 hours, counted
across every sweep and poll together. A video whose generation fails isn't retried for a day, so
one broken file can't keep taking that day's slots. A manual "Generate" button (Files page, or a
file's own detail page) runs one file immediately and ignores both limits.

Free options for both steps:

| | Provider | Notes |
|---|---|---|
| Speech-to-text | [Groq](https://console.groq.com/keys) | Generous free tier. Its own key field here, so a long transcription job can't eat the correctness check's quota |
| | OpenRouter | Free-tier availability varies by model |
| | [Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/) | ~10,000 free "neurons"/day; its per-request audio limit isn't documented — start with a short chunk length (Settings → Generate) and raise it only after testing against your own account |
| Translation | Groq / OpenRouter | The same chat-completions models the correctness check uses, configured separately here |
| | [Google Gemini](https://aistudio.google.com/apikey) | Free tier; Google may use free-tier prompts to improve its models — don't use it on anything sensitive |

Whisper itself can only translate speech straight to English, which is why any other target
language needs the separate LLM step above.

The `Vocabulary hint` field is worth a warning: it is fed to Whisper as decoder priming, not as
an instruction, so the model continues whatever shape of text it is given. Keep it a plain
comma-separated list of names. A labelled, sentence-shaped value ("Characters: ...") was observed
to make Whisper invent extra dialogue at the end of a clip, using words from the hint itself. The
field is flattened to one line and capped at 200 characters for that reason.


## Notes

- Everything is configured in the webapp, not env vars — `docker-compose.yml` only needs real
  Docker settings (volumes/port/TZ).
- The correctness check skips audio in a language Whisper isn't reliable for by default
  (configurable); a subtitle in a different language than the audio is machine-translated before
  comparing, so language alone never causes a false flag.
- Only one job (sweep/single) runs at a time; cancelling one stops it between files, not mid
  API call.
- Movies are supported for sync/correctness; `blacklist`/`remediate` are series-only for now.
