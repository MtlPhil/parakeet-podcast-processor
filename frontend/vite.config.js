import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev-server exposure is opt-in via frontend/.env.local (gitignored):
//   P3_DEV_HOST=0.0.0.0                  listen on all interfaces (LAN access)
//   P3_ALLOWED_HOSTS=p3.example.com      comma-separated extra hostnames
// The API has no authentication, so only expose it on networks you trust.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'P3_')
  const allowedHosts = env.P3_ALLOWED_HOSTS
    ? env.P3_ALLOWED_HOSTS.split(',').map((h) => h.trim()).filter(Boolean)
    : []

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: env.P3_DEV_HOST || 'localhost',
      allowedHosts,
      proxy: {
        '/api': 'http://127.0.0.1:8000',
      },
    },
  }
})
