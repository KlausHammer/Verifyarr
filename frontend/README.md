# verifyarr — frontend

React + TypeScript + Vite SPA for verifyarr's webapp. See the main project's `../README.md`
for what it does.

- `npm run dev` — local development (proxies `/api` to `http://127.0.0.1:8787`, see `vite.config.ts`)
- `npm run build` — builds to `dist/`, which `Dockerfile` copies into `verifyarr/web/static/`
  during the container build. FastAPI (`verifyarr/web/app.py`) serves it directly, including
  client-side-routing fallback.

`src/routes/` = pages, `src/api/` = types + fetch client, `src/components/` = shared components
(Layout/sidebar, status pills), `src/hooks/` = auth context + polling hooks, `src/styles/theme.css`
= the dark theme (CSS variables).
