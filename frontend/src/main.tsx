import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryCache, QueryClient, QueryClientProvider, type Query } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { copy as t } from './lib/copy'
import { useAuth } from './store/auth'
import { useQueryErrors } from './store/queryErrors'
import './index.css'

function isCurrentQuery(query: Pick<Query, 'queryKey' | 'queryHash'>) {
  const key = useAuth.getState().apiKey
  return key !== null && query.queryKey[1] === key
    && queryClient.getQueryCache().get(query.queryHash) === query
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (!isCurrentQuery(query)) return
      const message = error instanceof TypeError
        ? t.errors.network
        : error instanceof Error ? error.message : String(error)
      useQueryErrors.getState().report(query.queryHash, message)
    },
    onSuccess: (_data, query) => {
      if (!isCurrentQuery(query)) return
      useQueryErrors.getState().resolve(query.queryHash)
    },
  }),
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, staleTime: 10_000 },
  },
})

queryClient.getQueryCache().subscribe((event) => {
  if (event.type === 'removed') useQueryErrors.getState().resolve(event.query.queryHash)
})

useAuth.subscribe((state, previous) => {
  if (state.apiKey === previous.apiKey) return
  // API 在执行时读取凭证；账号切换必须先取消旧查询，再移除其缓存与重试入口。
  const privateQueries = { predicate: (query: Query) => query.queryKey[0] !== 'health' }
  void queryClient.cancelQueries(privateQueries)
  queryClient.removeQueries(privateQueries)
  useQueryErrors.getState().clear()
})

// 生产环境中 base 为 '/ui/'（由 FastAPI 挂载）；为 router 去掉末尾斜杠。
const basename = import.meta.env.BASE_URL.replace(/\/$/, '')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={basename}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
