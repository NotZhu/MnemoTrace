import { currentSession, isCurrentSession, useAuth } from '../store/auth'
import { copy as t } from '../lib/copy'
import { isValidApiKey } from './apiKey'
import type { AuditReport,
  AgentStatus,
  MemorySpace,
  OperationResult,
  ProcessingResult,
  Conflict,
  Focus,
  FactEdit,
  FactSource,
  FactWrite,
  FactMetadata,
  Category,
  GraphResponse,
  Health,
  MemoryDump,
  Policy,
  PolicyResponse,
  ProcessingStatus,
  RecallResult,
  RememberResult,
  SessionReport,
  SessionsIndex,
  ProfileResult,
} from '../types'

// 默认同源（FastAPI 同时托管 SPA 与 API）。当 API 部署在其他位置时，用
// VITE_API_BASE 覆盖。
const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export function apiBaseUrl(): string {
  if (typeof window === 'undefined') return BASE || 'http://localhost:8000'
  if (!BASE) return window.location.origin
  return new URL(BASE, window.location.origin).toString().replace(/\/$/, '')
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: { code?: string; stored_raw?: boolean; episode_ids?: string[]; operation_ids?: string[]; operations?: OperationResult[] },
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export class MemoryRevisionChangedError extends ApiError {
  constructor(message = '记录已变化，请重新加载') {
    super(409, message)
    this.name = 'MemoryRevisionChangedError'
  }
}

export class StaleSessionError extends ApiError {
  constructor() {
    super(409, t.errors.spaceChanged)
    this.name = 'StaleSessionError'
  }
}

function requestSession(publicRequest = false) {
  const session = currentSession()
  if (session.key !== null && !isValidApiKey(session.key)) {
    useAuth.getState().logout()
    throw new ApiError(400, t.errors.invalidApiKey)
  }
  const check = () => {
    if (!publicRequest && !isCurrentSession(session)) throw new StaleSessionError()
  }
  // 成功和失败都先检查归属；旧网络错误也不能成为新登录的失败提示。
  const wait = async <T>(pending: Promise<T>): Promise<T> => {
    let value: T
    try {
      value = await pending
    } catch (error) {
      check()
      throw error
    }
    check()
    return value
  }
  return { session, check, wait }
}

let scopeRequests = new AbortController()
useAuth.subscribe((state, previous) => {
  if (state.epoch === previous.epoch) return
  scopeRequests.abort()
  scopeRequests = new AbortController()
})

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const guard = requestSession(path === '/health')
  const { key, spaceId } = guard.session
  if (path.startsWith('/v1/') && !path.startsWith('/v1/spaces')) {
    if (!spaceId) throw new ApiError(422, '请先选择记忆空间')
    if (typeof init.body === 'string') {
      init = { ...init, body: JSON.stringify({ ...JSON.parse(init.body), space_id: spaceId }) }
    } else {
      path += (path.includes('?') ? '&' : '?') + new URLSearchParams({ space_id: spaceId })
    }
  }
  const signal = path === '/health' ? init.signal
    : init.signal ? AbortSignal.any([init.signal, scopeRequests.signal]) : scopeRequests.signal
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (key) headers.set('Authorization', `Bearer ${key}`)

  const res = await guard.wait(fetch(`${BASE}${path}`, { ...init, headers, signal }))
  // 旧请求的迟到响应不能退出新账号，也不能驱动新账号的写后回调。
  guard.check()

  if (res.status === 401) {
    // key 已过期/错误 — 丢弃它，让应用回到登录页。
    if (isCurrentSession(guard.session)) useAuth.getState().logout()
    throw new ApiError(401, t.errors.unauthorized)
  }
  if (!res.ok) {
    let code: string | undefined
    let failure: { code?: string; stored_raw?: boolean; episode_ids?: string[]; operation_ids?: string[]; operations?: OperationResult[] } | undefined
    let detail = t.errors.requestFailed(res.status)
    try {
      const body = await guard.wait(res.json())
      if (body?.detail) {
        code = body.detail.code
        if (typeof body.detail === 'object') failure = body.detail
        detail = typeof body.detail === 'string' ? body.detail : body.detail.message ?? JSON.stringify(body.detail)
      }
    } catch (error) {
      if (error instanceof StaleSessionError) throw error
      /* 非 JSON 的错误响应体 */
    }
    guard.check()
    if (res.status === 409 && code === 'memory_revision_changed') throw new MemoryRevisionChangedError(detail)
    if (failure?.stored_raw) detail += ' 原文已保存，恢复模型后请重试整合，无需重新提交原文。'
    throw new ApiError(res.status, detail, failure)
  }
  if (res.status === 204) return undefined as T
  const data = (await guard.wait(res.json())) as T
  guard.check()
  return data
}

const json = (body: unknown) => JSON.stringify(body)
const qs = (params: object) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params as Record<string, string | number | boolean | string[] | null | undefined>)) {
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, item)
    } else if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

export type OutcomeKind = 'decision' | 'finding' | 'lesson' | 'open_question'

export interface MemoriesQuery {
  categories?: Category[]
  outcome_kind?: OutcomeKind
  expected_revision?: string
  evaluated_at?: number
  facts_limit?: number
  facts_offset?: number
  episodes_limit?: number
  episodes_offset?: number
  status?: 'live' | 'superseded'
  q?: string
  include_sensitive?: boolean
  // 服务器端对两类事实的划分：会话结论 vs. 逐轮属性。放在服务器侧意味着
  // facts_page.total 已经是过滤后的计数。
  kind?: 'outcomes' | 'attributes'
}

export interface RecallOptions {
  answer?: boolean
  n_chunks?: number
  as_of?: number
  redact_sensitive?: boolean
}

export interface GraphQuery {
  q?: string
  live_only?: boolean
  include_sensitive?: boolean
  limit?: number
}

export interface SessionsQuery {
  limit?: number
  offset?: number
  q?: string
}

export interface RememberOptions {
  processing?: 'immediate' | 'buffered'
  session_id?: string
  extract?: boolean
}

export const api = {
  spaces: async (signal?: AbortSignal): Promise<MemorySpace[]> => {
    const spaces: MemorySpace[] = []
    let afterId: string | null = null
    do {
      const page: { spaces: MemorySpace[]; next_after_id: string | null } =
        await request(`/v1/spaces${qs({ after_id: afterId })}`, { signal })
      spaces.push(...page.spaces)
      afterId = page.next_after_id
    } while (afterId !== null)
    return spaces
  },
  createSpace: (name: string) => request<MemorySpace>('/v1/spaces', {
    method: 'POST', body: json({ name }),
  }),
  rememberMessages: (messages: {message_id: string; role: 'user' | 'assistant'; content: string;
      event_time?: number | null}[], sessionId: string, mode: 'live' | 'historical' = 'live',
      extract = true, signal?: AbortSignal) =>
    request<RememberResult & {accepted: number; messages: {episode_id: string; operation_ids: string[]}[]}>(
      '/v1/messages', { method: 'POST', body: json({messages, session_id: sessionId, mode, extract, processing: 'buffered'}), signal }),
  processingStatus: (sessionId?: string, signal?: AbortSignal, afterId?: string) =>
    request<ProcessingStatus>(`/v1/processing${qs({session_id: sessionId, after_id: afterId})}`, { signal }),
  retryOperation: (id: string, signal?: AbortSignal) => request<OperationResult>(`/v1/operations/${encodeURIComponent(id)}/retry`, {
    method: 'POST', body: json({}), signal,
  }),
  health: () => request<Health>('/health'),

  memories: (params: MemoriesQuery = {}, options: Pick<RequestInit, 'signal'> = {}) =>
    request<MemoryDump>(`/v1/memories${qs(params)}`, options),
  audit: (limit = 60) => request<AuditReport>(`/v1/audit?limit=${limit}`),
  sessions: (params: SessionsQuery = {}) => request<SessionsIndex>(`/v1/sessions${qs(params)}`),
  agentStatus: (sessionId?: string, signal?: AbortSignal) =>
    request<AgentStatus>(`/v1/agent/status${qs({ session_id: sessionId })}`, { signal }),
  sessionReport: (sessionId: string, includeSensitive = false, offset?: number, signal?: AbortSignal) =>
    request<SessionReport>(`/v1/sessions/report${qs({ session_id: sessionId, include_sensitive: includeSensitive, offset })}`, { signal }),
  graph: (params: GraphQuery = {}) => request<GraphResponse>(`/v1/graph${qs(params)}`),
  focus: () => request<Focus>('/v1/focus'),

  remember: (content: string, options: RememberOptions = {}) =>
    request<RememberResult>('/v1/remember', {
      method: 'POST',
      body: json({
        content,
        session_id: options.session_id ?? 'default',
        extract: options.extract ?? true,
        processing: options.processing ?? 'immediate',
      }),
    }),

  organizeSession: (session_id: string, signal?: AbortSignal) =>
    request<ProcessingResult>('/v1/processing/flush', {
      method: 'POST', body: json({ session_id }), signal,
    }),

  recall: (query: string, options: RecallOptions = {}) =>
    request<RecallResult>('/v1/recall', {
      method: 'POST',
      body: json({
        query,
        lean: true,
        n_chunks: options.n_chunks ?? 6,
        as_of: options.as_of,
        redact_sensitive: options.redact_sensitive ?? false,
      }),
    }),

  addFact: (fact: FactWrite) =>
    request<{ ok: boolean; id: string; text: string } & FactMetadata>('/v1/facts', {
      method: 'POST',
      body: json({ subject: 'user', ...fact }),
    }),

  // 实际修改内容后 `source` 为 'user'；只改敏感标记或分类时保留原来源。
  editFact: (id: string, patch: FactEdit) =>
    request<{ ok: boolean; id: string; text: string; source: FactSource } & FactMetadata>(`/v1/facts/${id}`, {
      method: 'PATCH',
      body: json(patch),
    }),

  deleteFact: (id: string) =>
    request<{ ok: boolean }>(`/v1/facts/${id}`, { method: 'DELETE' }),

  setFocus: (focus: Partial<Focus>) =>
    request<{ ok: boolean; focus: Focus }>('/v1/focus', { method: 'PUT', body: json(focus) }),

  profile: () => request<ProfileResult>('/v1/profile'),
  refreshProfile: () => request<OperationResult>('/v1/profile/refresh', { method: 'POST', body: '{}' }),

  policy: () => request<PolicyResponse>('/v1/policy'),

  setPolicy: (patch: Partial<Policy>) =>
    request<{ ok: boolean } & PolicyResponse>('/v1/policy', { method: 'PUT', body: json(patch) }),

  forget: () =>
    request<{ ok: boolean; message: string }>('/v1/forget', { method: 'POST', body: json({ confirm: true }) }),

  conflicts: () => request<{ conflicts: Conflict[] }>('/v1/conflicts'),

  resolveConflict: (id: string, keep: 'newer' | 'older' | 'both') =>
    request<{ ok: boolean }>(`/v1/conflicts/${id}/resolve`, { method: 'POST', body: json({ keep }) }),

  /** 触发浏览器下载记忆导出（默认安全；保留 Bearer 请求头）。 */
  async exportDownload(filenameHint = 'me', includeSensitive = false): Promise<void> {
    const guard = requestSession()
    const { key, spaceId } = guard.session
    if (!spaceId) throw new ApiError(422, '请先选择记忆空间')
    const qs = new URLSearchParams({ include_sensitive: String(includeSensitive), space_id: spaceId })
    const res = await guard.wait(fetch(`${BASE}/v1/export?${qs}`, {
      signal: scopeRequests.signal,
      headers: key ? { Authorization: `Bearer ${key}` } : undefined,
    }))
    guard.check()
    if (!res.ok) throw new ApiError(res.status, t.errors.exportFailed(res.status))
    const blob = await guard.wait(res.blob())
    guard.check()
    const url = URL.createObjectURL(blob)
    let a: HTMLAnchorElement | undefined
    try {
      a = document.createElement('a')
      a.href = url
      a.download = `mnemotrace_${filenameHint}_export.json`
      document.body.appendChild(a)
      guard.check()
      a.click()
    } finally {
      try { a?.remove() } finally { URL.revokeObjectURL(url) }
    }
  },
}
