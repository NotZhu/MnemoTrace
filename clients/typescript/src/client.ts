/**
 * MnemoTraceClient — 忆痕记忆服务器的官方 TypeScript/JavaScript SDK。
 *
 * 零运行时依赖：使用全局 `fetch`（Node 18+ 或浏览器）。客户端携带 Bearer key，
 * 身份、主空间与共享空间权限由服务器配置解析。
 *
 *   import { MnemoTraceClient } from 'mnemotrace-memory'
 *   const mnemotrace = new MnemoTraceClient({ baseUrl: 'http://localhost:8000', apiKey: 'sk-alice-123' })
 *   await mnemotrace.remember('I live in Shenzhen and work at Tencent.')
 *   const { context } = await mnemotrace.recall('where do I live?')
 */
export interface SpaceOptions { spaceId: string }

import type {
  ChatCompletion,
  ChatCompletionChunk,
  ChatCompletionCreateParams,
  OperationResult,
  MemorySpace,
  AgentStatus,
  Fact,
  AggregateFactsOptions,
  AggregateFactsResult,
  FollowPathResult,
  StructuredReadOptions,
  ForgetOptions,
  Focus,
  GraphResponse,
  Health,
  MemoryExport,
  MemoryDump,
  MemoryListOptions,
  MemoryStats,
  OkMessage,
  Policy,
  PolicyResponse,
  ProfileResult,
  RecallResult,
  RememberResult,
  ProcessingStatus,
  ProcessingFlushResult,
  SearchResult,
  SessionIndex,
  SessionListOptions,
  SessionReport,
} from './types'

/** 任何非 2xx 响应都会抛出此错误。`detail` 尽可能携带解析后的错误响应体。 */
export class MnemoTraceError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'MnemoTraceError'
  }
}

export interface MnemoTraceClientOptions {
  /** 服务器基础 URL。默认 'http://localhost:8000'。允许以 '/v1' 结尾，会被规整掉。 */
  baseUrl?: string
  /** Bearer 鉴权凭据；可访问的记忆空间由服务器配置决定。 */
  apiKey?: string
  /** 自定义 fetch 实现（如旧运行时环境下的 node-fetch）。默认使用全局 fetch。 */
  fetch?: typeof fetch
  /** 每个请求都会携带的额外请求头。 */
  headers?: Record<string, string>
}

/** 重载的 create()：`stream: true` 时返回流式响应，否则返回单条补全。 */
export interface ChatCompletionsCreate {
  (params: ChatCompletionCreateParams & { stream: true }): Promise<AsyncIterable<ChatCompletionChunk>>
  (params: ChatCompletionCreateParams & { stream?: false }): Promise<ChatCompletion>
}

export class MnemoTraceClient {
  readonly baseUrl: string
  private readonly apiKey?: string
  private readonly _fetch: typeof fetch
  private readonly extraHeaders: Record<string, string>

  /** OpenAI 兼容接口：`client.chat.completions.create({ messages })`。 */
  readonly chat: { completions: { create: ChatCompletionsCreate } }

  constructor(options: MnemoTraceClientOptions = {}) {
    const base = options.baseUrl ?? 'http://localhost:8000'
    // 容忍 base 已以 /v1 结尾的情况（两种写法都可用）
    this.baseUrl = base.replace(/\/$/, '').replace(/\/v1$/, '')
    this.apiKey = options.apiKey
    const f = options.fetch ?? (globalThis.fetch ? globalThis.fetch.bind(globalThis) : undefined)
    if (!f) {
      throw new Error('No global fetch found. Use Node 18+, a browser, or pass options.fetch.')
    }
    this._fetch = f
    this.extraHeaders = options.headers ?? {}
    this.chat = {
      completions: { create: this.createChatCompletion.bind(this) as ChatCompletionsCreate },
    }
  }

  // --- 底层请求封装 ----------------------------------------------------------
  private buildHeaders(json: boolean): Record<string, string> {
    const h: Record<string, string> = { ...this.extraHeaders }
    if (json) h['Content-Type'] = 'application/json'
    if (this.apiKey) h['Authorization'] = `Bearer ${this.apiKey}`
    return h
  }

  private async assertOk(res: Response): Promise<void> {
    if (res.ok) return
    let detail: unknown
    let message = `MnemoTrace request failed (${res.status})`
    try {
      const body = (await res.json()) as { detail?: unknown }
      detail = body
      if (body?.detail) {
        message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      }
    } catch {
      /* 非 JSON 的错误响应体 */
    }
    throw new MnemoTraceError(res.status, message, detail)
  }

  private async request<T>(path: string, init: RequestInit = {}, spaceId?: string): Promise<T> {
    if (spaceId !== undefined) {
      if (!spaceId.trim()) throw new MnemoTraceError(422, 'A space ID is required')
      if (typeof init.body === 'string') {
        init = { ...init, body: JSON.stringify({ ...JSON.parse(init.body), space_id: spaceId }) }
      } else {
        const separator = path.includes('?') ? '&' : '?'
        path += separator + new URLSearchParams({ space_id: spaceId })
      }
    }
    const hasBody = init.body != null
    const res = await this._fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { ...this.buildHeaders(hasBody), ...(init.headers as Record<string, string>) },
    })
    await this.assertOk(res)
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  private post<T>(path: string, body: object, spaceId?: string): Promise<T> {
    return this.request<T>(path, { method: 'POST', body: JSON.stringify(body) }, spaceId)
  }

  // --- 健康检查 ---------------------------------------------------------------
  health(): Promise<Health> {
    return this.request<Health>('/health')
  }

  listSpaces(options: { afterId?: string; limit?: number } = {}): Promise<{
    spaces: MemorySpace[]; next_after_id: string | null
  }> {
    const params = new URLSearchParams()
    if (options.afterId !== undefined) params.set('after_id', options.afterId)
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    return this.request(`/v1/spaces${params.size ? `?${params}` : ''}`)
  }

  createSpace(name: string): Promise<MemorySpace> {
    return this.post('/v1/spaces', { name })
  }

  renameSpace(spaceId: string, name: string): Promise<MemorySpace> {
    return this.request(`/v1/spaces/${encodeURIComponent(spaceId)}`, {
      method: 'PATCH', body: JSON.stringify({ name }),
    })
  }

  deleteSpace(spaceId: string, options: { confirm: true }): Promise<OkMessage> {
    if (!options?.confirm) throw new MnemoTraceError(400, 'Space deletion requires confirmation')
    return this.request(`/v1/spaces/${encodeURIComponent(spaceId)}?confirm=true`, { method: 'DELETE' })
  }

  rememberMessages(messages: import('./types.js').DialogueMessage[], options: SpaceOptions & {
    sessionId: string; processing?: 'immediate' | 'buffered';
    extract?: boolean; agentId?: string; timeout?: number
  }): Promise<import('./types.js').MessagesResult> {
    return this.post('/v1/messages', { messages, session_id: options.sessionId,
      processing: options.processing, extract: options.extract,
      agent_id: options.agentId, timeout: options.timeout }, options.spaceId)
  }

  remember(content: string, options: SpaceOptions & {
    sessionId?: string; extract?: boolean; eventTime?: number | null;
    processing?: 'immediate' | 'buffered'; agentId?: string; timeout?: number
  }): Promise<RememberResult> {
    return this.post('/v1/remember', {
      content, session_id: options.sessionId, extract: options.extract, processing: options.processing,
      agent_id: options.agentId, event_time: options.eventTime, timeout: options.timeout,
    }, options.spaceId)
  }

  getOperation(operationId: string, options: SpaceOptions): Promise<OperationResult> {
    return this.request(`/v1/operations/${encodeURIComponent(operationId)}`, {}, options.spaceId)
  }

  retryOperation(operationId: string, options: SpaceOptions): Promise<OperationResult> {
    return this.post(`/v1/operations/${encodeURIComponent(operationId)}/retry`, {}, options.spaceId)
  }

  processingStatus(options: SpaceOptions & {
    sessionId?: string; afterId?: string; limit?: number
  }): Promise<ProcessingStatus> {
    const query = new URLSearchParams()
    if (options.sessionId !== undefined) query.set('session_id', options.sessionId)
    if (options.afterId !== undefined) query.set('after_id', options.afterId)
    if (options.limit !== undefined) query.set('limit', String(options.limit))
    return this.request(`/v1/processing${query.size ? `?${query}` : ''}`, {}, options.spaceId)
  }

  flushProcessing(options: SpaceOptions & { sessionId?: string; timeout?: number;
    retryFailed?: boolean; includeDigests?: boolean }): Promise<ProcessingFlushResult> {
    return this.post('/v1/processing/flush', {
      session_id: options.sessionId, timeout: options.timeout,
      retry_failed: options.retryFailed, include_digests: options.includeDigests,
    }, options.spaceId)
  }

  getEpisode(episodeId: string, options: SpaceOptions): Promise<import('./types.js').OriginalEpisode> {
    return this.request(`/v1/episodes/${encodeURIComponent(episodeId)}`, {}, options.spaceId)
  }

  recall(query: string, options: ({ spaceId: string; spaceIds?: never } | { spaceIds: string[]; spaceId?: never }) & {
    nDigests?: number; asOf?: number; redactSensitive?: boolean; answer?: boolean
    state?: StructuredReadOptions["state"]; validAt?: number
  }): Promise<RecallResult> {
    return this.post('/v1/recall', {
      query, lean: true, n_digests: options.nDigests, answer: options.answer,
      as_of: options.asOf, state: options.state, valid_at: options.validAt, redact_sensitive: options.redactSensitive, space_ids: options.spaceIds,
    }, options.spaceId)
  }

  /** 审计某个会话贡献了哪些持久事实。敏感事实文本默认脱敏。 */
  sessionReport(
    sessionId: string,
    options: SpaceOptions & { includeSensitive?: boolean; limit?: number; offset?: number },
  ): Promise<SessionReport> {
    const params = new URLSearchParams({
      session_id: sessionId,
      include_sensitive: options.includeSensitive ? 'true' : 'false',
    })
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    if (options.offset !== undefined) params.set('offset', String(options.offset))
    return this.request<SessionReport>(`/v1/sessions/report?${params}`, {}, options.spaceId)
  }

  /** 不含内容的跨 agent/应用会话索引，用于记忆管理界面。 */
  sessions(options: SpaceOptions & SessionListOptions): Promise<SessionIndex> {
    const params = new URLSearchParams()
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    if (options.offset !== undefined) params.set('offset', String(options.offset))
    const query = options.query ?? options.q
    if (query !== undefined) params.set('q', query)
    const qs = params.toString()
    return this.request<SessionIndex>(`/v1/sessions${qs ? `?${qs}` : ''}`, {}, options.spaceId)
  }

  aggregateFacts(predicate: string, options: SpaceOptions & AggregateFactsOptions): Promise<AggregateFactsResult> {
    const { spaceId, ...filters } = options
    return this.post('/v1/aggregate', { predicate, ...filters }, spaceId)
  }

  followPath(anchor: string, predicates: string[], options: SpaceOptions & StructuredReadOptions): Promise<FollowPathResult> {
    const { spaceId, ...filters } = options
    return this.post('/v1/follow-path', { anchor, predicates, ...filters }, spaceId)
  }

  /** Retrieve candidates or the unique value of an explicit subject and predicate. */
  search(query: string, options: SpaceOptions & { asOf?: number; redactSensitive?: boolean;
    state?: StructuredReadOptions["state"]; validAt?: number; subject?: string; predicate?: string }): Promise<SearchResult> {
    return this.post<SearchResult>('/v1/recall', {
      query,
      lean: false,
      subject: options.subject, predicate: options.predicate,
      as_of: options.asOf ?? null,
      state: options.state, valid_at: options.validAt,
      redact_sensitive: options.redactSensitive ?? false,
    }, options.spaceId)
  }

  /** 该命名空间下存储的全部记忆，支持分页/过滤，用于用户自有记忆管理界面。 */
  memories(options: SpaceOptions & MemoryListOptions): Promise<MemoryDump> {
    const params = new URLSearchParams()
    for (const category of options.categories ?? []) params.append('categories', category)
    if (options.factsLimit !== undefined) params.set('facts_limit', String(options.factsLimit))
    if (options.factsOffset !== undefined) params.set('facts_offset', String(options.factsOffset))
    if (options.episodesLimit !== undefined) params.set('episodes_limit', String(options.episodesLimit))
    if (options.episodesOffset !== undefined) params.set('episodes_offset', String(options.episodesOffset))
    if (options.status !== undefined) params.set('status', options.status)
    if (options.kind !== undefined) params.set('kind', options.kind)
    if (options.outcomeKind !== undefined) params.set('outcome_kind', options.outcomeKind)
    if (options.expectedRevision !== undefined) params.set('expected_revision', options.expectedRevision)
    if (options.evaluatedAt !== undefined) params.set('evaluated_at', String(options.evaluatedAt))
    const query = options.query ?? options.q
    if (query !== undefined) params.set('q', query)
    if (options.includeSensitive !== undefined) {
      params.set('include_sensitive', options.includeSensitive ? 'true' : 'false')
    }
    const qs = params.toString()
    return this.request<MemoryDump>(`/v1/memories${qs ? `?${qs}` : ''}`, {}, options.spaceId)
  }

  /** 不含内容的命名空间统计，用于仪表盘；服务就绪检查使用 /ready。 */
  stats(options: SpaceOptions): Promise<MemoryStats> {
    return this.request<MemoryStats>('/v1/stats', {}, options.spaceId)
  }

  /** 不含内容的 agent/会话状态：命名空间、focus、计数以及建议的记忆操作。 */
  agentStatus(options: SpaceOptions & { sessionId?: string }): Promise<AgentStatus> {
    const qs = options.sessionId === undefined ? '' : `?session_id=${encodeURIComponent(options.sessionId)}`
    return this.request<AgentStatus>(`/v1/agent/status${qs}`, {}, options.spaceId)
  }

  /** 显式创建空间级画像刷新任务。 */
  refreshProfile(options: SpaceOptions): Promise<OperationResult> {
    return this.request<OperationResult>('/v1/profile/refresh', { method: 'POST', body: '{}' }, options.spaceId)
  }

  /** 读取结构化画像缓存及待更新状态。 */
  profile(options: SpaceOptions): Promise<ProfileResult> {
    return this.request<ProfileResult>('/v1/profile', {}, options.spaceId)
  }

  // --- 事实操作 ----------------------------------------------------------
  /** 更改事实的共享可见性。 */
  setFactSensitive(id: string, sensitive: boolean, options: SpaceOptions): Promise<{ ok: boolean; id: string; sensitive: boolean }> {
    return this.request(`/v1/facts/${encodeURIComponent(id)}/sensitivity`, { method: 'PATCH', body: JSON.stringify({ sensitive }) }, options.spaceId)
  }

  deleteFact(id: string, options: SpaceOptions): Promise<{ ok: boolean }> {
    return this.request(`/v1/facts/${encodeURIComponent(id)}`, { method: 'DELETE' }, options.spaceId)
  }

  // --- focus / policy / graph ----------------------------------------------
  getFocus(options: SpaceOptions): Promise<Focus> {
    return this.request<Focus>('/v1/focus', {}, options.spaceId)
  }

  setFocus(focus: Partial<Focus>, options: SpaceOptions): Promise<{ ok: boolean; focus: Focus }> {
    return this.request('/v1/focus', { method: 'PUT', body: JSON.stringify(focus) }, options.spaceId)
  }

  getPolicy(options: SpaceOptions): Promise<PolicyResponse> {
    return this.request<PolicyResponse>('/v1/policy', {}, options.spaceId)
  }

  setPolicy(patch: Partial<Policy>, options: SpaceOptions): Promise<{ ok: boolean } & PolicyResponse> {
    return this.request('/v1/policy', { method: 'PUT', body: JSON.stringify(patch) }, options.spaceId)
  }

  /** 语义图谱。默认返回可安全分享的版本；传入 includeSensitive=true 获取完整的所有者可见图谱。 */
  graph(options: SpaceOptions & { asOf?: number; includeSensitive?: boolean }): Promise<GraphResponse> {
    const params = new URLSearchParams()
    if (options.asOf !== undefined) params.set('as_of', String(options.asOf))
    if (options.includeSensitive !== undefined) {
      params.set('include_sensitive', options.includeSensitive ? 'true' : 'false')
    }
    const qs = params.toString()
    return this.request<GraphResponse>(`/v1/graph${qs ? `?${qs}` : ''}`, {}, options.spaceId)
  }

  // --- 导出 / 遗忘 ----------------------------------------------------------

  /** 数据导出。默认返回可安全分享的结构化导出；传入 includeSensitive=true 获取完整私有导出。 */
  export<T = MemoryExport>(options: SpaceOptions & { includeSensitive?: boolean }): Promise<T> {
    const includeSensitive = options.includeSensitive ?? false
    const qs = `?include_sensitive=${includeSensitive ? 'true' : 'false'}`
    return this.request<T>(`/v1/export${qs}`, {}, options.spaceId)
  }

  /** 清空该命名空间下的所有记忆（不可逆）；需要显式确认。 */
  forget(options: SpaceOptions & ForgetOptions): Promise<OkMessage> {
    if (!options?.confirm) {
      throw new MnemoTraceError(400, 'forget requires { confirm: true } because it permanently erases this namespace')
    }
    return this.post<OkMessage>('/v1/forget', { confirm: true }, options.spaceId)
  }

  // --- OpenAI 兼容 chat ------------------------------------------------------
  private async createChatCompletion(
    params: ChatCompletionCreateParams,
  ): Promise<ChatCompletion | AsyncIterable<ChatCompletionChunk>> {
    const res = await this._fetch(`${this.baseUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: this.buildHeaders(true),
      body: JSON.stringify(params),
    })
    await this.assertOk(res)
    if (params.stream) return parseSSE(res)
    return (await res.json()) as ChatCompletion
  }
}

/** 将 OpenAI 风格的 SSE 流解析为 ChatCompletionChunk。 */
async function* parseSSE(res: Response): AsyncGenerator<ChatCompletionChunk> {
  if (!res.body) throw new MnemoTraceError(500, 'streaming requested but the response has no body')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const event = buffer.slice(0, idx).trim()
        buffer = buffer.slice(idx + 2)
        if (!event.startsWith('data:')) continue
        const data = event.slice(5).trim()
        if (data === '[DONE]') return
        try {
          yield JSON.parse(data) as ChatCompletionChunk
        } catch {
          /* 跳过格式错误的分块 */
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
