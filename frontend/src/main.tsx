import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryCache, QueryClient, QueryClientProvider, type Query } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { copy as t } from './lib/copy'
import { currentSession, useAuth } from './store/auth'
import { StaleSessionError, MemoryRevisionChangedError } from './lib/api'
import { useQueryErrors } from './store/queryErrors'
import './index.css'

type QueryIdentity = Pick<Query, 'queryKey' | 'queryHash'>
const queryEpochs = new WeakMap<QueryIdentity, number>()

function isCurrentQuery(query: QueryIdentity) {
  const { key, spaceId, epoch } = currentSession()
  return key !== null && (query.queryKey[0] === 'spaces' || query.queryKey[1] === spaceId)
    && queryEpochs.get(query) === epoch
    && queryClient.getQueryCache().get(query.queryHash) === query
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (error instanceof StaleSessionError || !isCurrentQuery(query)) return
      if (error instanceof MemoryRevisionChangedError) {
        useQueryErrors.getState().resolve(query.queryHash)
        return
      }
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
  if (event.type === 'added') queryEpochs.set(event.query, currentSession().epoch)
  if (event.type === 'removed') useQueryErrors.getState().resolve(event.query.queryHash)
})

useAuth.subscribe((state, previous) => {
  if (state.epoch === previous.epoch) return
  // 同 Key 再次登录也要移除旧周期的查询、缓存与重试入口。
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
