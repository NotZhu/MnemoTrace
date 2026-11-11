export { useMemoryPages } from './memoryPages'
import { hashKey, useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'

import {
  api,
  type GraphQuery,
  type MemoriesQuery,
  type RecallOptions,
  type RememberOptions,
  type SessionsQuery,
} from '../lib/api'
import { currentSession, isCurrentSession, useAuth } from '../store/auth'
import type { FactEdit, FactWrite, Focus, Policy } from '../types'

// 查询 key 按登录 key 做命名空间隔离，切换账号时缓存不会串味。
const ns = () => useAuth.getState().spaceId ?? 'unselected'
export const qk = {
  memoriesRoot: () => ['memories', ns()] as const,
  memories: (params: MemoriesQuery = {}) => ['memories', ns(), params] as const,
  sessionsRoot: () => ['sessions', ns()] as const,
  sessions: (params: SessionsQuery = {}) => ['sessions', ns(), params] as const,
  agentStatus: (sessionId?: string) => ['agent-status', ns(), sessionId ?? null] as const,
  sessionReport: (sessionId: string) => ['session-report', ns(), sessionId] as const,
  graphRoot: () => ['graph', ns()] as const,
  graph: (params: GraphQuery = {}) => ['graph', ns(), params] as const,
  focus: () => ['focus', ns()] as const,
  policy: () => ['policy', ns()] as const,
  profile: () => ['profile', ns()] as const,
  sourceEpisodes: () => ['source-episodes', ns()] as const,
  conflicts: () => ['conflicts', ns()] as const,
  health: () => ['health'] as const,
  // 与其他 key 一样按命名空间隔离（见上面的 ns()）— 审计是按记忆空间进行的，
  // 不加命名空间的 key 在切换后会显示上一个空间的检查结果，连同它的一键批量删除。
  // 前缀 key：失效 ['audit', ns()] 即可匹配 Health 页保存的任意 limit。
  auditRoot: () => ['audit', ns()] as const,
  audit: (limit: number) => ['audit', ns(), limit] as const,
}

export function useHealth() {
  return useQuery({ queryKey: qk.health(), queryFn: api.health, staleTime: 30_000, retry: false })
}

function memoriesScope(params: MemoriesQuery) {
  // 只放行搜索词和请求条数变化；新筛选字段默认参与比较，避免跨可见范围借用整份响应。
  const { q: _q, facts_limit: _factsLimit, episodes_limit: _episodesLimit, ...scope } = params
  return hashKey([{
    ...scope,
    include_sensitive: params.include_sensitive ?? false,
    categories: params.categories?.length ? [...new Set(params.categories)].sort() : undefined,
  }])
}

export function useMemories(params: MemoriesQuery = {}, options: { keepPreviousData?: boolean } = {}) {
  const namespace = useAuth((s) => s.spaceId)
  return useQuery({
    queryKey: qk.memories(params), queryFn: () => api.memories(params), enabled: !!namespace, retry: false,
    placeholderData: options.keepPreviousData ? (previousData, previousQuery) => {
      const previousParams = previousQuery?.queryKey[2] as MemoriesQuery | undefined
      return previousQuery?.queryKey[1] === namespace && previousParams
        && memoriesScope(previousParams) === memoriesScope(params) ? previousData : undefined
    } : undefined,
  })
}

export function useSessions(params: SessionsQuery = {}, options: { keepPreviousData?: boolean } = {}) {
  const namespace = useAuth((s) => s.spaceId)
  return useQuery({
    queryKey: qk.sessions(params), queryFn: () => api.sessions(params), enabled: !!namespace, retry: false,
    // 搜索换词时保留本空间的列表，切换账号时不能显示其他空间的会话。
    placeholderData: options.keepPreviousData ? (previousData, previousQuery) =>
      previousQuery?.queryKey[1] === namespace ? previousData : undefined : undefined,
  })
}

export function useAgentStatus(sessionId?: string) {
  const enabled = !!useAuth((s) => s.spaceId)
  return useQuery({
    queryKey: qk.agentStatus(sessionId),
    queryFn: ({ signal }) => api.agentStatus(sessionId, signal),
    gcTime: sessionId ? 0 : undefined,
    enabled,
    retry: false,
    staleTime: 15_000,
  })
}

export function useSessionReport(sessionId: string | null | undefined, includeSensitive = false, offset = 0) {
  const enabled = !!useAuth((s) => s.spaceId) && !!sessionId
  return useQuery({
    queryKey: [...qk.sessionReport(sessionId ?? ''), includeSensitive, offset],
    queryFn: ({ signal }) => api.sessionReport(sessionId!, includeSensitive, offset, signal),
    gcTime: 0,
    enabled,
    retry: false,
  })
}

function graphScope(params: GraphQuery) {
  const { live_only: _liveOnly, ...scope } = params
  return hashKey([{ ...scope, include_sensitive: params.include_sensitive ?? false }])
}

export function useGraph(params: GraphQuery = {}, options: { keepPreviousData?: boolean } = {}) {
  const namespace = useAuth((s) => s.spaceId)
  return useQuery({
    queryKey: qk.graph(params), queryFn: () => api.graph(params), enabled: !!namespace, retry: false,
    // 仅当前开关可借用旧图；搜索、隐私及身份变化必须清除旧响应。
    placeholderData: options.keepPreviousData ? (previousData, previousQuery) => {
      const previousParams = previousQuery?.queryKey[2] as GraphQuery | undefined
      return previousQuery?.queryKey[1] === namespace && previousParams
        && graphScope(previousParams) === graphScope(params) ? previousData : undefined
    } : undefined,
  })
}

export function useFocus() {
  const enabled = !!useAuth((s) => s.spaceId)
  return useQuery({ queryKey: qk.focus(), queryFn: api.focus, enabled, retry: false })
}

function refreshAfterWrite(client: QueryClient, roots: readonly QueryKey[]) {
  const session = currentSession()
  if (session.spaceId === null || roots.some(key => key[1] !== session.spaceId)) return
  // Invalidation reuses pending first loads. Cancel them even when no data has been committed yet.
  void Promise.all(roots.map(queryKey => client.cancelQueries({ queryKey }))).then(() => {
    if (!isCurrentSession(session)) return
    return Promise.all(roots.map(queryKey => client.invalidateQueries({ queryKey })))
  })
}

/** 使一次写入可能影响的所有查询失效（facts、profile、graph）。 */
function useInvalidateMemory() {
  const qc = useQueryClient()
  return () => refreshAfterWrite(qc, [
    qk.memoriesRoot(), qk.sessionsRoot(), qk.graphRoot(), qk.focus(), qk.profile(),
    // 审计结果也派生自事实，必须重新核对批量操作前显示的数量。
    qk.auditRoot(),
  ])
}

/** 原始 episode，仅在需要把某条事实的 provenance id 解析回源文本时才拉取。 */
export function useSourceEpisodes(enabled: boolean) {
  const namespace = useAuth((s) => s.spaceId)
  return useQuery({
    queryKey: qk.sourceEpisodes(),
    queryFn: () => api.memories({ facts_limit: 0, episodes_limit: 200, include_sensitive: true }),
    enabled: enabled && !!namespace,
    retry: false,
  })
}

export function useAudit(limit = 60) {
  return useQuery({
    queryKey: qk.audit(limit),
    queryFn: () => api.audit(limit),
  })
}

export function useProfile() {
  const enabled = !!useAuth((s) => s.spaceId)
  return useQuery({ queryKey: qk.profile(), queryFn: api.profile, enabled, retry: false,
    refetchInterval: query => ['pending', 'running', 'retry_wait'].includes(query.state.data?.refresh_state ?? '') ? 1000 : false })
}


export function useRetryOperation() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: (id: string) => api.retryOperation(id), onSuccess: invalidate })
}

export function useRefreshProfile() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: () => api.refreshProfile(), onSuccess: invalidate })
}

export function useConflicts() {
  const enabled = !!useAuth((s) => s.spaceId)
  return useQuery({ queryKey: qk.conflicts(), queryFn: api.conflicts, enabled, retry: false })
}

export function useResolveConflict() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, keep }: { id: string; keep: 'newer' | 'older' | 'both' }) =>
      api.resolveConflict(id, keep),
    onSuccess: () => refreshAfterWrite(qc, [qk.conflicts(), qk.memoriesRoot(), qk.profile(), qk.graphRoot()]),
  })
}


export function useRemember() {
  const invalidate = useInvalidateMemory()
  return useMutation({
    mutationFn: (req: string | ({ content: string } & RememberOptions)) =>
      typeof req === 'string' ? api.remember(req) : api.remember(req.content, req),
    onSuccess: invalidate,
  })
}

export function useRememberMessages(signal?: AbortSignal) {
  const invalidate = useInvalidateMemory()
  return useMutation({
    mutationFn: (req: {messages: Parameters<typeof api.rememberMessages>[0]; sessionId: string;
      mode?: 'live' | 'historical'}) => api.rememberMessages(req.messages, req.sessionId, req.mode, signal),
    onSuccess: invalidate,
  })
}

export function useOrganizeSession(signal?: AbortSignal) {
  const invalidate = useInvalidateMemory()
  return useMutation({
    mutationFn: (session_id: string) => api.organizeSession(session_id, signal),
    onSuccess: () => {
      invalidate()
    },
  })
}

export function useRecall() {
  return useMutation({
    mutationFn: (req: string | ({ query: string } & RecallOptions)) =>
      typeof req === 'string' ? api.recall(req) : api.recall(req.query, req),
  })
}

export function useAddFact() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: (fact: FactWrite) => api.addFact(fact), onSuccess: invalidate })
}

export function useEditFact() {
  const invalidate = useInvalidateMemory()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: FactEdit }) => api.editFact(id, patch),
    onSuccess: invalidate,
  })
}

export function useDeleteFact() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: (id: string) => api.deleteFact(id), onSuccess: invalidate })
}

export function useSetFocus() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: (focus: Partial<Focus>) => api.setFocus(focus), onSuccess: invalidate })
}

export function useForget() {
  const invalidate = useInvalidateMemory()
  return useMutation({ mutationFn: () => api.forget(), onSuccess: invalidate })
}

export function usePolicy() {
  const enabled = !!useAuth((s) => s.spaceId)
  return useQuery({ queryKey: qk.policy(), queryFn: api.policy, enabled, retry: false })
}

export function useSetPolicy() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (patch: Partial<Policy>) => api.setPolicy(patch),
    onSuccess: () => refreshAfterWrite(qc, [qk.policy(), qk.memoriesRoot()]),
  })
}
