import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev server proxies the API + generated media to the FastAPI backend,
// so the frontend can always talk to relative /api and /media paths.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/media': { target: 'http://127.0.0.1:8000', changeOrigin: true }
    }
  }
})
