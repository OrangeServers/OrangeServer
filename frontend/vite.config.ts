// =============================================================================
// Vite 配置 (ti3-TS: 迁移到 TS)
// =============================================================================
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发服务器的后端地址从当前 mode 的 .env 读取。
// REVIEW-14 P1-5: 默认值清空（防硬编码内网 IP 泄露后端拓扑）
//   缺失或不合法时直接报错，强制开发者配置 .env.development
// 生产构建只生成同源静态资源，不需要代理目标。
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const API_SERVER: string = env.VITE_API_TARGET || ''
  if (command === 'serve' && !API_SERVER) {
    throw new Error('[vite.config.ts] VITE_API_TARGET 未配置')
  }
  if (command === 'serve' && !/^https?:\/\//i.test(API_SERVER)) {
    throw new Error('[vite.config.ts] VITE_API_TARGET 必须是 HTTP(S) URL')
  }
  return {
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    port: 5173,
    proxy: {
      // 开发容器使用同源 WebSocket，Vite 将 Upgrade 请求转发给 backend。
      '^/local(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false,
        ws: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            const ip = (req.socket?.remoteAddress || '127.0.0.1').replace('::ffff:', '')
            proxyReq.setHeader('X-Real-IP', ip)
            proxyReq.setHeader('X-Forwarded-For', ip)
          })
          proxy.on('error', (err, req) => {
            console.error('[Proxy Error]', req.method, req.url, err.message)
          })
        }
      },
      '^/server(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
      '^/account(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
      '^/mail(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
      '^/auth(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
      '^/ai(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
      // SETUP-WIZARD: 首次部署向导 API
      '^/setup/api(?:/|$)': {
        target: API_SERVER,
        changeOrigin: false
      },
    }
  }
  }
})
