import { defineConfig, type Connect, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

const consoleBase = '/ui/'

function consoleRootRedirect(): Plugin {
  const redirect: Connect.NextHandleFunction = (req: Connect.IncomingMessage & { url?: string; method?: string }, res, next) => {
    const url = req.url ?? ''
    const pathname = url.split('?', 1)[0]
    // React Router 的总览链接为 /ui；在 Vite 的 base 检查前规范化，保留查询参数。
    if ((req.method === 'GET' || req.method === 'HEAD') && pathname === consoleBase.slice(0, -1)) {
      res.writeHead(302, { Location: consoleBase + url.slice(pathname.length), 'Cache-Control': 'no-cache' })
      res.end()
      return
    }
    next()
  }
  return {
    name: 'console-root-redirect',
    configureServer: (server) => { server.middlewares.use(redirect) },
    configurePreviewServer: (server) => { server.middlewares.use(redirect) },
  }
}

// The console is served by the FastAPI memory server under `/ui/` in production
// (mnemotrace/server/app.py mounts the built `dist/`). In dev, Vite proxies the API
// to the running server so the SPA talks to the real backend with zero CORS.
export default defineConfig({
  plugins: [consoleRootRedirect(), react()],
  base: consoleBase,
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    // Windows 的原子写入临时目录可能短暂锁住文件，监听这些目录会导致 EBUSY 退出。
    watch: { ignored: ['**/.*.tmpdir', '**/.*.tmpdir/**'] },
    proxy: {
      '/v1': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
