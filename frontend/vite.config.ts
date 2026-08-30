import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Under 'npm run dev' proxies API-kald til den lokalt kørende Python-backend (uvicorn på
    // :8787, se verifyarr/web/__main__.py) — samme adfærd som i produktion, hvor FastAPI selv
    // server det byggede dist/ (se app.py).
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
