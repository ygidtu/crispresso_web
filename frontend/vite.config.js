import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 静态资源相对路径，便于 Flask 直接 serve 构建产物
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:5000',
    },
  },
})
