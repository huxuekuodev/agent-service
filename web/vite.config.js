import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前后端分离：开发时前端 5173 → 代理 API 到后端 8001（避免跨域）
const API_TARGET = 'http://localhost:8001'
const API_PREFIXES = ['/sessions', '/monitor', '/health', '/knowledge']

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [
        prefix,
        { target: API_TARGET, changeOrigin: true },
      ])
    ),
  },
})
