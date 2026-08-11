import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前后端分离：开发时前端 5173 → 代理到后端 8001（避免跨域）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/sessions': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
