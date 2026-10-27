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
    // 查询已被移除时只清理失效提示；重试不改变页面或编辑组件生命周期。
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
    <div className="mb-6">
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
