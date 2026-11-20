import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The gateway is the only backend. GATEWAY_PORT matches `GATEWAY_PORT=8010 make dev-all`.
// The proxy keeps the browser's Host header (threads-co.localhost:5173), which is how the
// gateway knows which shop is being visited. *.localhost names are allowed by Vite by default.
const gateway = `http://127.0.0.1:${process.env.GATEWAY_PORT || 8000}`

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: gateway, changeOrigin: false } },
  },
})
