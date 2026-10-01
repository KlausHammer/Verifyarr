import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Under 'npm run dev', API calls are proxied to the locally running Python backend (uvicorn on
    // :8787, see verifyarr/web/__main__.py) -- the same behaviour as in production, where FastAPI itself
    // serves the built dist/ (see app.py).
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
