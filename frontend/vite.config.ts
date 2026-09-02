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
      '/search':        'http://localhost:8420',
      '/documents':     'http://localhost:8420',
      '/clusters':      'http://localhost:8420',
      '/runs':          'http://localhost:8420',
      '/tags':          'http://localhost:8420',
      '/trends':        'http://localhost:8420',
      '/ingestion':     'http://localhost:8420',
      '/images':        'http://localhost:8420',
      '/reading-lists': 'http://localhost:8420',
      '/settings':      'http://localhost:8420',
      '/tag-training': {
        target: 'http://localhost:8420',
        // LLM pseudo-label runs many sequential Ollama calls
        timeout: 30 * 60 * 1000,
        proxyTimeout: 30 * 60 * 1000,
      },
    },
  },
  build: { outDir: 'dist' },
})
