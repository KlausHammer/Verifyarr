# Verifyarr UI conventions

Verifyarr is a self-hosted media tool (it checks and re-times subtitles). Design it like the
other *arr tools (Sonarr, Radarr, Bazarr, Prowlarr) in layout and density - but in Verifyarr's
own colors and components below, never their logos or brand colors:

- Fixed left sidebar with the page list (background `var(--bg-sidebar)`), content to the right.
- Each page: a top toolbar row of actions (`.btn`, primary action as `.btn btn-primary`), then
  the content - mostly dense tables of files/episodes with a status column, or settings forms.
- Settings are forms in `.card`s, one section per card, grouped by tab.
- Dark theme only. Compact 13-14px text, little whitespace, no hero sections or illustrations.

## Setup

No provider or wrapper component. Everything is styled by `styles.css` (tokens + global classes
+ component styles); load it and set the page background:
`<body style="background: var(--bg); color: var(--text); font-family: var(--font)">`.
Without the dark background, buttons and inputs look wrong on white.

## Styling idiom: global classes + CSS variables

Use these classes; do not invent new class names. Glue layout (flex/grid/gaps) is inline styles.

| Purpose | Classes |
|---|---|
| Surfaces | `card` (elevated panel, 16px padding, border, radius) |
| Buttons | `btn`, `btn btn-primary` (teal), `btn btn-danger` (red), add `btn-sm` for small |
| Form rows | `field` (label + control, max 480px), `field-hint` (small help text under it) |
| Status | `pill` + one of `pill-ok` (green), `pill-bad` (red), `pill-warn` (yellow), `pill-info` (blue), `pill-muted` (grey) - or the `StatusPill` component |
| Text | `text-dim`, `text-faint`, `mono` |
| Feedback | `error-banner`, `spinner` |

Tokens (`var(--*)`): `--bg`, `--bg-elevated`, `--bg-sidebar`, `--bg-hover`, `--border`, `--text`,
`--text-dim`, `--text-faint`, `--accent` (teal), `--accent-dim`, `--green`, `--yellow`, `--red`,
`--blue`, `--grey`, `--radius`, `--font`, `--mono`. There are no table or sidebar classes: style
tables with `--border` row lines, `--text-dim` headers and `--bg-hover` row hover.

## Components

`StatusPill` (any sync/check/job status string), `ConfirmDialog` (full-window confirm overlay;
`danger` for destructive actions), `LanguageMultiSelect` (language picker for a `.field`),
`CopyLogButton` (header button of a log panel). Props are in each `<Name>.d.ts`; read
`styles.css` and `_ds_bundle.css` for the exact class rules.

## Example

```jsx
const { StatusPill } = window.VerifyarrUI
<div className="card">
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
    <h3 style={{ margin: 0 }}>Season 2</h3>
    <button className="btn btn-primary btn-sm">Scan now</button>
  </div>
  <div style={{ display: 'flex', gap: 8 }}>
    <StatusPill value="already in sync" />
    <StatusPill value="SUSPECT" />
  </div>
</div>
```
