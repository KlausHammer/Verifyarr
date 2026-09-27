Design the complete Verifyarr web UI so an ordinary self-hoster can install it, set it up and
live with it without reading any documentation. Use the Verifyarr design system in this project
(its README conventions, tokens, classes and components). Deliver every screen below with its
empty, loading, error and "job running" states, plus the clickable flows at the end.

## What Verifyarr is

A self-hosted companion to Sonarr, Radarr and Bazarr. It listens to the audio of every movie and
episode (Whisper speech recognition), checks each subtitle against it, and:
- fixes timing automatically: a constant offset, a framerate/speed mismatch (24 vs 23.976, PAL
  25), slow drift, or a subtitle where parts are shifted;
- flags what it must not fix: the wrong episode's text, missing parts, cut/edited versions,
  many swapped lines, noisy timing. Flagged files can be quarantined, blacklisted in Bazarr, or
  replaced automatically (Bazarr fetches candidates; Verifyarr tests them; if none passes, the
  original is kept and stays flagged).
It runs unattended on a small home server (often an Intel N100). Users open it to see what needs
attention, to confirm it did the right thing, and to change settings. Most are not technical
about subtitles - every verdict must say in plain words what happened and what to do.

## Design direction

- Same structure and density as Sonarr/Radarr/Bazarr/Prowlarr: fixed left sidebar, a toolbar of
  actions at the top of each page, dense sortable tables, settings as forms in cards. Use
  Verifyarr's own colors, components and name - never another project's logo or brand colors.
- Dark theme only. English UI copy. Desktop first; at tablet/phone width the sidebar collapses to
  a menu button and tables become stacked rows.
- Reuse the design-system components: StatusPill for every status, ConfirmDialog for anything
  slow or destructive, LanguageMultiSelect for language choice, CopyLogButton on every log.
- A persistent "job running" indicator in the sidebar (progress, click to open the run).
- Toasts for action results ("Re-check started", "Restored to ...").
- Relative times ("3 min ago") with the exact time on hover.
- Every setting has a short "?" tip. Settings pages have a Simple / Advanced toggle; Simple shows
  only what most people change.
- Accessible: visible keyboard focus, 4.5:1 contrast for text, status never shown by color alone.

## Screens

1. **Create account** (first launch): username, password, repeat. Then straight into the wizard.
2. **Setup wizard** (new, skippable at any step, reachable later from Settings):
   1) Media folders: Movies folder and Series folder (folder browser), with a check that each
      exists and how many files it holds.
   2) Subtitle languages to check (LanguageMultiSelect; empty = all).
   3) Bazarr: URL + API key + "Test connection" (success/failure inline), path mapping explained.
   4) Speech recognition: local Whisper (default, no key) or a cloud provider + API key.
   5) What to do with a bad subtitle: Only flag / Quarantine / Blacklist in Bazarr / Fetch a
      replacement automatically (explain each in one line; mention "original is kept if no
      replacement passes").
   6) Schedule: nightly scan time, or manual only. Finish -> first scan starts -> Dashboard.
3. **Login.**
4. **Dashboard** (new; today "/" just redirects to Stats): library health (% of subtitles in
   sync), "Needs attention" list grouped by reason with counts (links into filtered Files),
   "Recently fixed" (last 10 with what was changed), current or last job with progress, next
   scheduled scan, primary action "Scan library". Empty state before the first scan.
5. **Movies** and **Series**: one row per title - title, videos, subtitles found, Ok / Suspect /
   Missing counts, last processed; search; per-row "Scan"; toolbar "Scan all" / "Rescan
   everything" (ConfirmDialog: it can take hours).
6. **Files**: one row per subtitle - episode/file, language, sync result, check result, score,
   swapped lines, last processed. Filters: result, sync status, language, free text. Bulk select
   (all matching, not just the page) with Re-check now / Quarantine / Blacklist / Fetch
   replacement. Pagination. Each row shows the plain-language verdict (table below), not raw text.
7. **File detail**: header (title, S01E02, language, paths); a verdict card - one sentence of what
   happened, one of what to do, and the matching action button; "What changed" (e.g. moved
   +12.3 s, rescaled 23.976 -> 24, 3 sections re-timed); collapsible "Technical details" with the
   raw note; check history table (time, result, score, audio language); actions: Re-check,
   Fetch replacement, Generate with Whisper, Quarantine, Blacklist.
8. **Activity**: jobs table (#, type, target, status, started, duration, files processed /
   changed / flagged / errors); filter by status.
9. **Job detail**: live progress bar and counters, live log (auto-scroll, pause on scroll-up,
   CopyLogButton), Cancel (ConfirmDialog), link to the files it changed or flagged.
10. **Stats**: match rate over time, score distribution, average score by language, movies vs
    series, swapped-line findings, recent jobs. Charts in the dark palette.
11. **Quarantine & Backups**: two tables (file, original location, size, time) with Restore
    (ConfirmDialog when it would overwrite the current subtitle).
12. **Bazarr blacklist**: what Verifyarr blacklisted (file, language, provider, outcome, time).
13. **Settings** (sidebar sub-menu, Simple/Advanced toggle): General (folders, languages,
    backups), Sync (Whisper mode sampled/full, clips per 10 min; Advanced: min change, framerate
    fix, drift threshold, full transcript on problems, anchor check, re-sync from anchors),
    Speech recognition (provider, local Whisper, keys; Advanced: model, GPU, threads), Generate
    missing subtitles, Automation ("What runs" table, action for bad subtitles, remediation
    attempts, dry run), Bazarr, Scheduling, Log (live app log, level), Account (password).

## Plain-language verdicts (use these labels and texts)

| Raw result | Pill | What happened | What to do |
|---|---|---|---|
| ok / already in sync | green "In sync" | Timing and text match the audio. | Nothing. |
| fixed (Δ12.3s ...) | green "Fixed" | Moved to match the audio (by how much). | Nothing; open to see what changed. |
| fixed (framerate ... / rate ...) | green "Fixed" | Made for another framerate or speed; rescaled. | Nothing. |
| SUSPECT - text does not match | red "Wrong subtitle" | The text is not what is spoken (likely another episode or release). | Fetch replacement. |
| SUSPECT - part out of sync | red "Partly out of sync" | Part of the episode sits at another offset (often a cut/edited version). | Fetch replacement. |
| SUSPECT - missing part | red "Missing lines" | No subtitles for a stretch where there is speech. | Fetch replacement. |
| SUSPECT - past the end | red "Doesn't fit this video" | Lines run past the end of the audio. | Fetch replacement. |
| SUSPECT - many swapped lines | red "Lines out of order" | Many two-line entries are in the wrong order; timing cannot be trusted. | Fetch replacement. |
| SUSPECT - noisy timing | red "Unreliable timing" | Lines are individually off; no single fix exists. | Fetch replacement. |
| missing | grey "No subtitle" | No subtitle for this language. | Generate with Whisper, or wait for Bazarr. |
| generated | blue "Generated" | Made from the audio by Whisper. | Nothing. |
| skipped | grey "Skipped" | Audio is in a language the check doesn't cover. | Nothing, or change settings. |
| unknown | yellow "Couldn't check" | Nothing could be verified (e.g. no speech recognized). | Re-check. |
| replacement failed | red + note | No replacement passed after N tries; the original was kept. | Try again later or pick one in Bazarr. |

## Data you can rely on (do not invent other features)

Files: title, season/episode, language, paths, sizes, last processed, sync result + largest
shift + number of re-timed sections, check result + score (0-1), swapped lines fixed/flagged,
note (technical text), automatic action taken, check history. Jobs: type, target, status,
started/finished, totals (processed, changed, flagged, errors, generated), live log stream,
cancel. Library: per title counts. Stats: summary and match rate over time. Quarantine/backups:
list + restore (no delete). Bazarr: test connection, blacklist history. Settings: the groups above.

## Flows to prototype

1. First launch: create account -> wizard -> first scan running -> Dashboard fills in.
2. Dashboard "Needs attention: 3 Wrong subtitle" -> filtered Files -> File detail -> Fetch
   replacement -> toast -> the job appears in the sidebar indicator -> result.
3. Files: select all "Partly out of sync" -> bulk Fetch replacement (ConfirmDialog).
4. Settings: Simple -> Advanced toggle on the Sync tab; save with success message.
