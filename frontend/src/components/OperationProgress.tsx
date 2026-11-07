import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../store/auth'
import type { OperationResult } from '../types'

const phase: Record<OperationResult['phase'], string> = {
  queued: '排队', generate: '生成产物', embed: '生成向量', commit: '提交结果', compensate: '恢复处理', done: '完成',
}

function label(operation: OperationResult) {
  if (operation.state === 'failed' || operation.state === 'retry_wait') {
    return operation.kind === 'extract' ? '核心处理失败' : '辅助任务失败'
  }
  if (operation.state === 'cancelled') return '已作废'
  if (operation.core_committed) return operation.kind === 'extract' ? '核心处理完成' : '辅助任务完成'
  return operation.state === 'pending' && operation.phase === 'queued' ? '已受理' : '处理中'
}

export function OperationProgress({ sessionId, sessionSignal }: { sessionId: string; sessionSignal: AbortSignal }) {
  const spaceId = useAuth(state => state.spaceId)
  const [afterId, setAfterId] = useState<string | undefined>()
  const progress = useQuery({
    queryKey: ['operations', spaceId, sessionId, afterId],
    queryFn: ({ signal }) => api.processingStatus(sessionId, signal, afterId),
    refetchInterval: 3_000,
    gcTime: 0,
    retry: false,
  })
  const retry = useMutation({
    mutationFn: (id: string) => api.retryOperation(id, sessionSignal),
    onSuccess: () => {
      if (!sessionSignal.aborted) void progress.refetch()
    },
  })
  return (
    <section aria-label="处理进度" className="space-y-2 rounded-xl border border-line p-4">
      <h3 className="text-sm font-medium">处理进度</h3>
      {progress.isError && <p role="alert" className="text-brand-rose">读取失败。<button onClick={() => void progress.refetch()}>重试</button></p>}
      {!progress.isError && progress.data?.operations.length === 0 && <p className="text-sm text-ghost">暂无操作</p>}
      <ul className="space-y-2">
        {progress.data?.operations.map(operation => (
          <li key={operation.operation_id} className="flex flex-wrap items-center gap-2 text-sm">
            <span>{label(operation)} · {phase[operation.phase]}</span>
            <code className="text-xs text-ghost">{operation.operation_id}</code>
            {operation.error_code && <span className="text-brand-rose">{operation.error_code}</span>}
            {(operation.state === 'failed' || operation.state === 'retry_wait') && (
              <button type="button" disabled={retry.isPending} onClick={() => retry.mutate(operation.operation_id)}
                className="rounded border border-line px-2 py-1">重试操作</button>
            )}
          </li>
        ))}
      </ul>
      {retry.isError && <p role="alert" className="text-sm text-brand-rose">{retry.error.message}</p>}
      {afterId && <button type="button" onClick={() => setAfterId(undefined)}>回到首页</button>}
      {progress.data?.next_after_id && (
        <button type="button" onClick={() => setAfterId(progress.data?.next_after_id ?? undefined)}>下一页</button>
      )}
    </section>
  )
}
