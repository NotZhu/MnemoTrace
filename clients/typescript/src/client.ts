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
import type {
  ChatCompletion,
  ChatCompletionChunk,
  ChatCompletionCreateParams,
  CloseSessionResult,
  AgentStatus,
  Fact,
  FactInput,
  FactPatch,
  TopicMetadata,
  ForgetOptions,
  Focus,
  GraphResponse,
  Health,
  ImportParams,
  ImportResult,
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

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const hasBody = init.body != null
    const res = await this._fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { ...this.buildHeaders(hasBody), ...(init.headers as Record<string, string>) },
    })
    await this.assertOk(res)
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  private post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, { method: 'POST', body: body === undefined ? '{}' : JSON.stringify(body) })
  }

  // --- 健康检查 ---------------------------------------------------------------
  health(): Promise<Health> {
    return this.request<Health>('/health')
  }

  // --- 写入 / 读取记忆 -------------------------------------------------------
  /**
   * 存储一条消息并将其整合为 bi-temporal 事实。
   * `space` 写入共享/agent 命名空间而非调用者自己的主命名空间（需要服务器端通过
   * MNEMOTRACE_SPACES 授予写权限）。
   */
  remember(
    content: string,
    options: { sessionId?: string; scope?: string; space?: string } = {},
  ): Promise<RememberResult> {
    return this.post<RememberResult>('/v1/remember', {
      content,
      session_id: options.sessionId ?? 'default',
      scope: options.scope ?? 'auto',
      space: options.space,
    })
  }

  /**
   * 检索一小段相关、带日期的上下文用于作答（lean 读路径）。
   * `spaces` 读取并融合多个命名空间（如 agent 自己的 + 共享团队空间）；每个命名空间
   * 都必须对该 key 的 MNEMOTRACE_SPACES 主体可读。
   */
  recall(
    query: string,
    options: {
      nChunks?: number
      sessionId?: string
      asOf?: number
      redactSensitive?: boolean
      spaces?: string[]
    } = {},
  ): Promise<RecallResult> {
    return this.post<RecallResult>('/v1/recall', {
      query,
      lean: true,
      n_chunks: options.nChunks ?? 6,
      session_id: options.sessionId ?? null,
      as_of: options.asOf ?? null,
      redact_sensitive: options.redactSensitive ?? false,
      spaces: options.spaces,
    })
  }

  /** 结束一个会话/任务：整合待处理的 episode、生成摘要、清空工作记忆并持久化。 */
  closeSession(
    sessionId = 'default',
    options: { summarize?: boolean; clearWorking?: boolean } = {},
  ): Promise<CloseSessionResult> {
    return this.post<CloseSessionResult>('/v1/sessions/close', {
      session_id: sessionId,
      summarize: options.summarize ?? true,
      clear_working: options.clearWorking ?? true,
    })
  }

  /** 审计某个会话贡献了哪些持久事实。敏感事实文本默认脱敏。 */
  sessionReport(
    sessionId: string,
    options: { includeSensitive?: boolean } = {},
  ): Promise<SessionReport> {
    const params = new URLSearchParams({
      session_id: sessionId,
      include_sensitive: options.includeSensitive ? 'true' : 'false',
    })
    return this.request<SessionReport>(`/v1/sessions/report?${params}`)
  }

  /** 不含内容的跨 agent/应用会话索引，用于记忆管理界面。 */
  sessions(options: SessionListOptions = {}): Promise<SessionIndex> {
    const params = new URLSearchParams()
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    if (options.offset !== undefined) params.set('offset', String(options.offset))
    const query = options.query ?? options.q
    if (query !== undefined) params.set('q', query)
    const qs = params.toString()
    return this.request<SessionIndex>(`/v1/sessions${qs ? `?${qs}` : ''}`)
  }

  /** 直接回答单个事实性问题（记忆中没有答案时选择拒答）。 */
  search(query: string, options: { asOf?: number; redactSensitive?: boolean } = {}): Promise<SearchResult> {
    return this.post<SearchResult>('/v1/recall', {
      query,
      lean: false,
      as_of: options.asOf ?? null,
      redact_sensitive: options.redactSensitive ?? false,
    })
  }

  /** 该命名空间下存储的全部记忆，支持分页/过滤，用于用户自有记忆管理界面。 */
  memories(options: MemoryListOptions = {}): Promise<MemoryDump> {
    const params = new URLSearchParams()
    if (options.topic !== undefined) params.set('topic', options.topic)
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
    return this.request<MemoryDump>(`/v1/memories${qs ? `?${qs}` : ''}`)
  }

  /** 不含内容的命名空间统计，用于仪表盘；服务就绪检查使用 /ready。 */
  stats(): Promise<MemoryStats> {
    return this.request<MemoryStats>('/v1/stats')
  }

  /** 不含内容的 agent/会话状态：命名空间、focus、计数以及建议的记忆操作。 */
  agentStatus(options: { sessionId?: string } = {}): Promise<AgentStatus> {
    const qs = options.sessionId === undefined ? '' : `?session_id=${encodeURIComponent(options.sessionId)}`
    return this.request<AgentStatus>(`/v1/agent/status${qs}`)
  }

  /** 合成后的用户画像 + 关键事实。 */
  profile(): Promise<ProfileResult> {
    return this.request<ProfileResult>('/v1/profile')
  }

  // --- 可编辑的事实 ----------------------------------------------------------
  /** 断言一条权威事实（用户来源；绝不会被静默覆盖）。 */
  addFact(fact: FactInput): Promise<{ ok: boolean; id: string; text: string } & TopicMetadata> {
    return this.post('/v1/facts', { subject: 'user', ...fact })
  }

  updateFact(id: string, patch: FactPatch): Promise<{ ok: boolean; id: string; text: string } & TopicMetadata> {
    return this.request(`/v1/facts/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  }

  deleteFact(id: string): Promise<{ ok: boolean }> {
    return this.request(`/v1/facts/${encodeURIComponent(id)}`, { method: 'DELETE' })
  }

  // --- focus / policy / graph ----------------------------------------------
  getFocus(): Promise<Focus> {
    return this.request<Focus>('/v1/focus')
  }

  setFocus(focus: Partial<Focus>): Promise<{ ok: boolean; focus: Focus }> {
    return this.request('/v1/focus', { method: 'PUT', body: JSON.stringify(focus) })
  }

  getPolicy(): Promise<PolicyResponse> {
    return this.request<PolicyResponse>('/v1/policy')
  }

  setPolicy(patch: Partial<Policy>): Promise<{ ok: boolean } & PolicyResponse> {
    return this.request('/v1/policy', { method: 'PUT', body: JSON.stringify(patch) })
  }

  /** 语义图谱。默认返回可安全分享的版本；传入 includeSensitive=true 获取完整的所有者可见图谱。 */
  graph(options: { asOf?: number; includeSensitive?: boolean } = {}): Promise<GraphResponse> {
    const params = new URLSearchParams()
    if (options.asOf !== undefined) params.set('as_of', String(options.asOf))
    if (options.includeSensitive !== undefined) {
      params.set('include_sensitive', options.includeSensitive ? 'true' : 'false')
    }
    const qs = params.toString()
    return this.request<GraphResponse>(`/v1/graph${qs ? `?${qs}` : ''}`)
  }

  // --- 批量导入 / 导出 / 遗忘 -------------------------------------------------
  /** 一次性批量导入历史记录（ChatGPT 导出、OpenAI messages、JSONL、transcript）。 */
  import(params: ImportParams): Promise<ImportResult> {
    return this.post<ImportResult>('/v1/import', params)
  }

  /** 数据导出。默认返回可安全分享的结构化导出；传入 includeSensitive=true 获取完整私有导出。 */
  export<T = MemoryExport>(options: { includeSensitive?: boolean } = {}): Promise<T> {
    const includeSensitive = options.includeSensitive ?? false
    const qs = `?include_sensitive=${includeSensitive ? 'true' : 'false'}`
    return this.request<T>(`/v1/export${qs}`)
  }

  /** 清空该命名空间下的所有记忆（不可逆）；需要显式确认。 */
  forget(options: ForgetOptions): Promise<OkMessage> {
    if (!options?.confirm) {
      throw new MnemoTraceError(400, 'forget requires { confirm: true } because it permanently erases this namespace')
    }
    return this.post<OkMessage>('/v1/forget', { confirm: true })
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
