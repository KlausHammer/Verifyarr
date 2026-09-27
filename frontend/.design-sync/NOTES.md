# design-sync notes (verifyarr frontend)

- The frontend is an app, not a library: no dist/ and no .d.ts. `.design-sync/entry.ts` re-exports
  the components as named exports (they are default exports); `componentSrcMap` lists them and
  `dtsPropsFor` carries hand-written props (source has inline types only).
- Synced: StatusPill, ConfirmDialog, LanguageMultiSelect, CopyLogButton (user's scope, 2026-09-27).
  Not synced: Layout (router + login + job polling) and FolderBrowser (calls the /api/browse API).
- User's design direction (2026-09-27): designs should follow the *arr family's layout
  (Sonarr/Radarr/Bazarr/Prowlarr) -- written into conventions.md for the design agent.
- Global class vocabulary (.btn, .card, .field, .pill-*, .text-dim ...) lives in
  src/styles/theme.css, shipped via cfg.cssEntry.
- Build (from frontend/): stage scripts per the skill, then
  `node .ds-sync/resync.mjs --config .design-sync/config.json --node-modules ./node_modules
  --entry ./.design-sync/entry.ts --out ./ds-bundle [--remote .design-sync/.cache/remote-sync.json]`.
  Render check uses playwright 1.60.0 (matches the cached chromium-1223); install it into .ds-sync.
- Previews sit on the app's dark surfaces (`.card`, or `var(--bg)`): on the harness's white
  cards the components looked broken. ConfirmDialog is a fixed full-window overlay - its
  preview wraps it in a 300px box with `transform: translateZ(0)` so the overlay stays inside.
- Known render warns: none (the ConfirmDialog RENDER_THIN went away with the contained box).

## Re-sync risks
- `dtsPropsFor` is hand-written from the component sources: a prop added/renamed in
  src/components/*.tsx does not reach the .d.ts until this config is updated.
- `.design-sync/entry.ts` lists the synced components by hand; a new shared component must be
  added there AND to `componentSrcMap`.
- conventions.md names classes/tokens from src/styles/theme.css; renaming one there makes the
  header lie - re-run the name check against ds-bundle/_ds_bundle.css.
