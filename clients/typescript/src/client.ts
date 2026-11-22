import type {
  FactChangeListQuery,
  FactChangePage,
  MessagePage,
  MessageResponse,
  OperationFactChangePage,
  OperationHandle,
  OperationListQuery,
  OperationPage,
  OperationResponse,
  PageQuery,
  RecallRequest,
  RecallResponse,
  RememberAndWaitOptions,
  RememberMessage,
  RememberOptions,
  RememberSubmission,
  SessionResponse,
  TerminalOperation,
  WaitOperationOptions,
} from './types'

const REQUEST_TIMEOUT_MS = 45_000
const SUBMIT_TIMEOUT_MS = 15_000
const SUBMIT_RETRY_DELAYS_MS: readonly (number | undefined)[] = [500, 1_000]
const WAIT_TIMEOUT_MS = 240_000
const POLL_INTERVAL_MS = 1_000

export interface MnemoTraceClientOptions {
  baseUrl: string
  apiKey: string
  fetch?: typeof globalThis.fetch
}

export class MnemoTraceError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status?: number,
    public readonly requestId?: string,
    public readonly cause?: MnemoTraceError,
  ) {
    super(message)
    this.name = 'MnemoTraceError'
  }
}

export class OperationWaitTimeout extends MnemoTraceError {
  constructor(public readonly handle: OperationHandle) {
    super('等待任务完成超时', 'operation_wait_timeout')
    this.name = 'OperationWaitTimeout'
  }
}

interface RequestOptions {
  method?: 'POST'
  body?: string
  idempotencyKey?: string
  timeoutMs?: number
}

function isResourceId(id: unknown): id is string {
  return typeof id === 'string' && id.length > 0 && !/[^A-Za-z0-9._~-]/.test(id)
    && id !== '.' && id !== '..'
}

function resourceSegment(id: string): string {
  if (!isResourceId(id)) {
    throw new TypeError('请使用 HTTP v1 返回的资源标识')
  }
  return id
}

function spacePath(spaceId: string): string {
  return `/v1/spaces/${resourceSegment(spaceId)}`
}

function operationPath({ spaceId, operationId }: OperationHandle): string {
  return `${spacePath(spaceId)}/operations/${resourceSegment(operationId)}`
}

function withQuery(
  path: string, query: Record<string, string | number | boolean | null | undefined>,
): string {
  const params = new URLSearchParams()
  for (const [name, value] of Object.entries(query)) {
    if (value != null) params.set(name, String(value))
  }
  const search = params.toString()
  return search ? `${path}?${search}` : path
}

function validateTimeout(timeoutMs: number): void {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 2_147_483_647) {
    throw new RangeError('超时须为 1 至 2147483647 毫秒的整数')
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, Math.ceil(ms)))
}

function isTerminal(operation: OperationResponse): operation is TerminalOperation {
  return operation.status === 'succeeded' || operation.status === 'failed'
}

function invalidResponse(response: Response): MnemoTraceError {
  return new MnemoTraceError(
    '服务返回了不符合 HTTP v1 契约的响应',
    'invalid_response',
    response.status,
    response.headers.get('X-Request-ID') ?? undefined,
  )
}

function requestTimeout(): MnemoTraceError {
  return new MnemoTraceError('HTTP 请求超时', 'request_timeout')
}

function validateOperation(operation: OperationResponse, response: Response): void {
  if (!operation || !isResourceId(operation.operation_id)
    || !['queued', 'running', 'succeeded', 'failed'].includes(operation.status)) {
    throw invalidResponse(response)
  }
}

export class MnemoTraceClient {
  readonly #baseUrl: string
  readonly #apiKey: string
  readonly #fetch: typeof globalThis.fetch

  constructor({ baseUrl, apiKey, fetch: fetchImpl }: MnemoTraceClientOptions) {
    const url = new URL(baseUrl)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password
      || url.search || url.hash) {
      throw new TypeError('baseUrl 须为不含凭据、查询或片段的 HTTP(S) 地址')
    }
    if (typeof apiKey !== 'string' || !apiKey.trim()) {
      throw new TypeError('apiKey 须为非空字符串')
    }
    this.#baseUrl = url.href.replace(/\/+$/, '')
    this.#apiKey = apiKey
    this.#fetch = fetchImpl ?? globalThis.fetch.bind(globalThis)
  }

  async createSession(spaceId: string): Promise<SessionResponse> {
    const { data } = await this.#request<SessionResponse>(`${spacePath(spaceId)}/sessions`, {
      method: 'POST',
    })
    return data
  }

  async remember(
    spaceId: string,
    sessionId: string,
    messages: readonly RememberMessage[] | string,
    { idempotencyKey, submitTimeoutMs = SUBMIT_TIMEOUT_MS }: RememberOptions,
  ): Promise<RememberSubmission> {
    if (typeof idempotencyKey !== 'string' || idempotencyKey.length < 1
      || idempotencyKey.length > 128 || /[^!-~]/.test(idempotencyKey)) {
      throw new TypeError('idempotencyKey 须包含 1 至 128 个可见 ASCII 字符')
    }
    const path = `${spacePath(spaceId)}/sessions/${resourceSegment(sessionId)}/remember`
    const body = JSON.stringify({
      messages: typeof messages === 'string' ? [{ role: 'user', content: messages }] : messages,
    })
    validateTimeout(submitTimeoutMs)
    const deadline = performance.now() + submitTimeoutMs
    let outcomeUnknown = false

    for (let attempt = 0; ; attempt++) {
      try {
        const { data: operation, response } = await this.#request<OperationResponse>(path, {
          method: 'POST',
          body,
          idempotencyKey,
          timeoutMs: deadline - performance.now(),
        })
        validateOperation(operation, response)
        const handle = { spaceId, operationId: operation.operation_id }
        if (response.status !== 202 || operation.session_id !== sessionId
          || response.headers.get('Location') !== operationPath(handle)) {
          throw invalidResponse(response)
        }
        return { handle, operation }
      } catch (error) {
        if (!(error instanceof MnemoTraceError)) throw error
        const uncertain = error.code === 'transport_error' || error.code === 'request_timeout'
          || (error.status === 503 && error.code === 'commit_outcome_unknown')
        const queueFull = error.status === 503 && error.code === 'remember_queue_full'
        outcomeUnknown ||= uncertain
        const delay = SUBMIT_RETRY_DELAYS_MS[attempt]
        if ((uncertain || queueFull) && delay !== undefined && deadline - performance.now() > delay) {
          await sleep(delay)
          continue
        }
        if (outcomeUnknown) {
          throw new MnemoTraceError(
            '提交结果不明，可用原幂等键和原参数恢复',
            'submit_outcome_unknown',
            error.status,
            error.requestId,
            error,
          )
        }
        throw error
      }
    }
  }

  async getOperation(handle: OperationHandle): Promise<OperationResponse> {
    return this.#getOperation(handle, REQUEST_TIMEOUT_MS)
  }

  async listOperations(spaceId: string, options: OperationListQuery = {}): Promise<OperationPage> {
    const path = withQuery(`${spacePath(spaceId)}/operations`, options)
    const { data } = await this.#request<OperationPage>(path)
    return data
  }

  async listOperationFactChanges(
    handle: OperationHandle,
    options: FactChangeListQuery = {},
  ): Promise<OperationFactChangePage> {
    const path = withQuery(`${operationPath(handle)}/fact-changes`, options)
    const { data } = await this.#request<OperationFactChangePage>(path)
    return data
  }

  async waitOperation(
    handle: OperationHandle,
    { timeoutMs = WAIT_TIMEOUT_MS }: WaitOperationOptions = {},
  ): Promise<TerminalOperation> {
    validateTimeout(timeoutMs)
    const deadline = performance.now() + timeoutMs
    while (true) {
      const remaining = deadline - performance.now()
      if (remaining <= 0) throw new OperationWaitTimeout(handle)

      let operation: OperationResponse
      try {
        operation = await this.#getOperation(handle, remaining)
      } catch (error) {
        if (error instanceof MnemoTraceError && error.code === 'request_timeout') {
          throw new OperationWaitTimeout(handle)
        }
        throw error
      }
      if (isTerminal(operation)) return operation

      const untilRetry = operation.next_retry_at
        ? Date.parse(operation.next_retry_at) - Date.now()
        : 0
      const delay = untilRetry > 0 ? untilRetry : POLL_INTERVAL_MS
      await sleep(Math.min(delay, Math.max(0, deadline - performance.now())))
    }
  }

  async rememberAndWait(
    spaceId: string,
    sessionId: string,
    messages: readonly RememberMessage[] | string,
    { timeoutMs = WAIT_TIMEOUT_MS, ...submit }: RememberAndWaitOptions,
  ): Promise<TerminalOperation> {
    validateTimeout(timeoutMs)
    const { handle, operation } = await this.remember(spaceId, sessionId, messages, submit)
    return isTerminal(operation) ? operation : this.waitOperation(handle, { timeoutMs })
  }

  async recall(spaceId: string, request: RecallRequest): Promise<RecallResponse> {
    const { data } = await this.#request<RecallResponse>(`${spacePath(spaceId)}/recall`, {
      method: 'POST',
      body: JSON.stringify(request),
    })
    return data
  }

  async getMessage(spaceId: string, messageId: string): Promise<MessageResponse> {
    const path = `${spacePath(spaceId)}/messages/${resourceSegment(messageId)}`
    const { data } = await this.#request<MessageResponse>(path)
    return data
  }

  async listFactChanges(
    spaceId: string, factId: string, options: FactChangeListQuery = {},
  ): Promise<FactChangePage> {
    const path = withQuery(`${spacePath(spaceId)}/facts/${resourceSegment(factId)}/changes`, options)
    const { data } = await this.#request<FactChangePage>(path)
    return data
  }

  async listSessionMessages(
    spaceId: string, sessionId: string, options: PageQuery = {},
  ): Promise<MessagePage> {
    const path = withQuery(`${spacePath(spaceId)}/sessions/${resourceSegment(sessionId)}/messages`, options)
    const { data } = await this.#request<MessagePage>(path)
    return data
  }

  async #getOperation(handle: OperationHandle, timeoutMs: number): Promise<OperationResponse> {
    const { data, response } = await this.#request<OperationResponse>(operationPath(handle), {
      timeoutMs,
    })
    validateOperation(data, response)
    if (data.operation_id !== handle.operationId) throw invalidResponse(response)
    return data
  }

  async #request<T>(
    path: string,
    { method, body, idempotencyKey, timeoutMs = REQUEST_TIMEOUT_MS }: RequestOptions = {},
  ): Promise<{ data: T; response: Response }> {
    if (timeoutMs <= 0) throw requestTimeout()
    const headers = new Headers({ Authorization: `Bearer ${this.#apiKey}` })
    if (body !== undefined) headers.set('Content-Type', 'application/json')
    if (idempotencyKey !== undefined) headers.set('Idempotency-Key', idempotencyKey)
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        reject(requestTimeout())
        controller.abort()
      }, Math.ceil(timeoutMs))
    })
    try {
      return await Promise.race([timeout, this.#fetchJson<T>(path, {
        method,
        headers,
        body,
        signal: controller.signal,
        redirect: 'error',
      })])
    } finally {
      clearTimeout(timer)
    }
  }

  async #fetchJson<T>(path: string, init: RequestInit): Promise<{ data: T; response: Response }> {
    try {
      const response = await this.#fetch(this.#baseUrl + path, init)
      const data: unknown = await response.json().catch((error: unknown) => {
        if (error instanceof SyntaxError) return undefined
        throw error
      })
      if (!response.ok) {
        const problem = data as { code?: unknown; request_id?: unknown } | null | undefined
        throw new MnemoTraceError(
          `HTTP 请求失败（${response.status}）`,
          typeof problem?.code === 'string' ? problem.code : 'http_error',
          response.status,
          typeof problem?.request_id === 'string'
            ? problem.request_id
            : response.headers.get('X-Request-ID') ?? undefined,
        )
      }
      if (data == null) throw invalidResponse(response)
      return { data: data as T, response }
    } catch (error) {
      if (error instanceof MnemoTraceError) throw error
      throw new MnemoTraceError('HTTP 请求传输失败', 'transport_error')
    }
  }
}
