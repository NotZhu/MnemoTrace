import { create } from 'zustand'

type QueryFailure = { key: string; message: string }

type QueryErrorsState = {
  failures: QueryFailure[]
  report: (key: string, message: string) => void
  resolve: (key: string) => void
  clear: () => void
}

// queryHash 包含账号凭证，只能留在内存；移除此前版本持久化的错误与重试标识。
try {
  if (typeof window !== 'undefined') window.sessionStorage.removeItem('mnemotrace.query-errors')
} catch { /* 存储不可用时仍使用内存错误状态。 */ }

export const useQueryErrors = create<QueryErrorsState>((set) => ({
  failures: [],
  // 同一个查询重复失败时移动到末尾，置顶条展示最近一次错误。
  report: (key, message) => set((state) => {
    const failures = [...state.failures.filter((failure) => failure.key !== key), { key, message }]
    return { failures }
  }),
  resolve: (key) => set((state) => {
    if (!state.failures.some((failure) => failure.key === key)) return state
    const failures = state.failures.filter((failure) => failure.key !== key)
    return { failures }
  }),
  clear: () => {
    set({ failures: [] })
  },
}))
