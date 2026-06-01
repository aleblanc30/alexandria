/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  test: {
    environment: 'happy-dom',
    globals: false,
    include: ['src/**/*.test.ts'],
  },
  server: {
    port: 5173,
    proxy: {
      '/search':        'http://localhost:8000',
      '/documents':     'http://localhost:8000',
      '/clusters':      'http://localhost:8000',
      '/runs':          'http://localhost:8000',
      '/tags':          'http://localhost:8000',
      '/trends':        'http://localhost:8000',
      '/ingestion':     'http://localhost:8000',
      '/images':        'http://localhost:8000',
      '/reading-lists': 'http://localhost:8000',
    },
  },
  build: { outDir: 'dist' },
})
