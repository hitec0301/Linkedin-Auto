import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the front end runs on 5173 and the API on 8000. Proxying
// rather than pointing the browser at another origin keeps the session cookie
// first-party in dev exactly as it is in production, so nothing about auth
// behaves differently between the two.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
