import { RefreshCw } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'

import { useQueryErrors } from '../store/queryErrors'
import { useAuth } from '../store/auth'
import { ErrorBanner } from './ui'

export function GlobalErrorBanner() {
  const failures = useQueryErrors((state) => state.failures)
  const queryClient = useQueryClient()
  const latest = failures[failures.length - 1]
  if (!latest) return null

  const retry = () => {
    // 刷新后页面内容没有挂载，失败的查询可能不在缓存里；此时清掉持久化错误，
    // 让 Outlet 重新挂载并重新请求。仍在缓存里的错误则主动重拉。
    const namespace = useAuth.getState().apiKey
    const failed = queryClient.getQueryCache().getAll().filter((query) =>
      namespace !== null && query.queryKey[1] === namespace && query.state.status === 'error')
    if (!failed.length) {
      useQueryErrors.getState().clear()
      return
    }
    void Promise.allSettled(failed.map((query) => query.fetch()))
  }

  return (
    <div className="sticky top-[72px] z-10 mb-6" role="alert">
      <ErrorBanner
        message={latest.message}
        action={(
          <button
            type="button"
            onClick={retry}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-brand-rose/30 px-2.5 py-1 text-xs text-brand-rose transition hover:bg-brand-rose/10"
          >
            <RefreshCw className="h-3.5 w-3.5" /> 重试
          </button>
        )}
      />
    </div>
  )
}
