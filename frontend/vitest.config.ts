// =============================================================================
// Vitest 配置（M1/S3 最小前端单测）
// 独立于 vite.config.ts：后者要求 VITE_API_TARGET 开发环境变量，单测不需要。
// =============================================================================
import { defineConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/__tests__/**/*.test.ts'],
  },
})
