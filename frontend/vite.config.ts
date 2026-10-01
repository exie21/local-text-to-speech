import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const projectRoot = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, projectRoot, 'API_PROXY_TARGET')
  const apiTarget = process.env.API_PROXY_TARGET || env.API_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': {
          target: apiTarget,
        },
      },
    },
  }
})
