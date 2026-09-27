import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

declare const process: { env: Record<string, string | undefined> }

export default defineConfig({
  plugins: [react()],
  server: {
    // 固定 origin：头像与访问密钥存在 localStorage，按 origin 隔离，
    // 端口被占时若静默跳号等于换了一个 origin，数据会"凭空丢失"
    host: '127.0.0.1',
    port: 3000,
    strictPort: true,
    proxy: {
      // MOZ_API_PORT 只给"前端指向沙箱后端"这种验收场景用；不设就是用户日常那台 8000
      '/api': {
        target: `http://127.0.0.1:${process.env.MOZ_API_PORT || '8000'}`,
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
})