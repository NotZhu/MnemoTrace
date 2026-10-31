/**
 * 与 忆痕 HTTP API（mnemotrace/server/app.py）对应的类型定义。
 * 这些就是服务器实际返回的响应结构，因此 SDK 调用具备完整类型。
 */

export interface Health {
  ok: boolean
  ready: boolean
  service: string
  auth_mode: 'api_keys' | 'open' | 'disabled'
  anonymous_allowed: boolean
  embedder: string
  llm_configured: boolean
  answerer_configured: boolean
  storage: string
  users_hot: number
  max_hot_users: number
  max_hot_facts: number
}

export interface ExtractionFailure {
  code: 'llm_not_configured' | 'buffered_processing_disabled' | 'processing_incomplete' | 'extraction_failed' | 'extraction_invalid_response' | 'extraction_truncated'
  message: string
  stored_raw: boolean
  episode_ids: string[]
}

export interface SummarizationResult {
  status: ProcessingState | 'not_needed'
  error_code: string | null
}

export interface SummaryProcessingStatus {
  counts: Record<ProcessingState, number>
  jobs: Array<{ episode_id: string; session_id: string; state: ProcessingState;
    attempts: number; error_code: string | null; completed_through: number | null }>
}

export interface OriginalEpisode {
  id: string
  space: string
  session_id: string
  speaker: string
  event_time: number
  ingested_at: number
  content: string
}

export interface RememberResult {
  summarization: SummarizationResult
  ok: boolean
  scope?: 'long' | 'working' | 'mixed' | 'none'
  queued?: boolean
  processing?: 'pending' | 'completed'
  episode_id?: string
  extracted?: number
  working_added?: number
  total_facts?: number
  kind?: string
}

export type ProcessingState = 'pending' | 'running' | 'retry_wait' | 'failed' | 'completed' | 'cancelled'

export interface ProcessingJob {
  episode_id: string
  session_id: string
  scope: 'auto' | 'long' | 'working'
  state: ProcessingState
  attempts: number
  window_attempts: number
  completed_through: number
  source_length: number
  content_hash: string
  queued_at: number
  updated_at: number
  next_attempt_at: number
  error_code: string | null
  forced: boolean
}

export interface ProcessingStatus {
  summarization?: SummaryProcessingStatus
  enabled: boolean
  generation: string | null
  session_id: string | null
  counts: Record<ProcessingState, number>
  jobs: ProcessingJob[]
  recovery_error?: boolean
}

export interface ProcessingFlushResult extends Partial<ProcessingStatus> {
  ok: boolean
  status: 'completed' | 'failed' | 'timed_out'
}

/** 检索到的精简上下文 — 用于作答的一小段带日期切片（recall）。 */
export interface RecallResult {
  context: string
  tokens_est: number
  as_of: number | null
  redacted_sensitive: boolean
  full_tokens?: number
  answer?: string
}

/** 直接给出的事实性答案及支撑事实（search）。 */
export interface SearchResult {
  answer: string
  facts: string[]
  as_of: number | null
  redacted_sensitive: boolean
}

export interface CloseSessionResult {
  ok: boolean
  session_id: string
  episodes: number
  pending_consolidated: number
  facts_added: number
  duplicates: number
  invalidated: number
  summaries: number
  reflected: number
  working_cleared: number
  /** 本次会话新提炼的结论。未变更的会话重复关闭不会新增；设置 `MNEMOTRACE_SESSION_OUTCOMES=0` 时不运行；未配置模型而请求提炼时返回错误。 */
  outcomes?: number
}

export interface SessionReportFact {
  id: string
  text: string
  display: string
  valid_at: string
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  category: string
  /** Omitted for redacted rows. */
  topic?: Topic
  topic_source?: TopicSource
  sensitive: boolean
  redacted: boolean
  provenance: string[]
  subject?: string
  predicate?: string
  object?: string
}

export interface SessionReport {
  ok: boolean
  user: string
  session_id: string
  include_sensitive: boolean
  episodes: number
  episodes_consolidated: number
  extraction_failures?: number
  episodes_pending: number
  working_live: number
  facts_added: number
  facts_redacted: number
  facts: SessionReportFact[]
}

export interface SessionIndexItem {
  id: string
  episodes: number
  episodes_consolidated: number
  extraction_failures?: number
  episodes_pending: number
  facts_added: number
  facts_sensitive: number
  working_live: number
  summaries: number
  first_event_at: number | null
  first_event_at_h: string | null
  last_event_at: number | null
  last_event_at_h: string | null
}

export interface SessionIndex {
  ok: boolean
  user: string
  sessions: SessionIndexItem[]
  page: MemoryPage<SessionIndexItem>
  next_offset: number | null
}

export interface SessionListOptions {
  limit?: number
  offset?: number
  /** 对 session id 的文本搜索。 */
  query?: string
  /** query 的别名，与 HTTP 参数名一致。 */
  q?: string
}

export type FactStatus = 'live' | 'superseded'
export type FactSource = 'extracted' | 'user'

export type Topic = 'food' | 'travel' | 'music' | 'film_tv' | 'sports' | 'reading' | 'hobbies'
  | 'environment' | 'health' | 'work' | 'learning' | 'technology' | 'finance' | 'relationships' | 'unknown'
export type TopicSource = 'unset' | 'rule' | 'llm' | 'user'
export interface TopicMetadata {
  topic: Topic
  topic_source: TopicSource
}

export interface Fact extends TopicMetadata {
  id: string
  text: string
  display?: string
  subject: string
  predicate: string
  object: string
  /** YYYY-MM-DD（生效起始日） */
  valid_at: string
  /** YYYY-MM-DD，仍有效时为 null */
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  supersedes: string | null
  category?: string
  sensitive?: boolean
  redacted?: boolean
  salience: number
  provenance: string[]
  /** 会话结论把依据内嵌在 `text` 中（"statement （依据：why）"）；服务器会把依据
   *  拆分到这里。普通属性事实为空字符串。 */
  why?: string
}

export interface EpisodeView {
  id: string
  summarization: SummarizationResult
  date: string
  session: string
  content: string
  summary: string
}

export interface MemoryCounts {
  episodes: number
  facts_live: number
  facts_superseded: number
  summaries: number
  /** 属于会话结论的存活事实。与上面两项一样，按未过滤集合统计。 */
  facts_outcomes?: number
}

export type MemoryStatusFilter = 'live' | 'current' | 'superseded' | 'old' | 'history'

export interface MemoryPage<T> {
  offset: number
  limit: number | null
  total: number
  next_offset: number | null
  has_more: boolean
  items: T[]
}

export type OutcomeKind = 'decision' | 'finding' | 'lesson' | 'open_question'

export interface MemoryListOptions {
  /** Exact stored main topic. unknown includes manual and unassigned unknown facts. */
  topic?: Topic
  outcomeKind?: OutcomeKind
  /** Pair with evaluatedAt from the first page; a changed commit returns HTTP 409. */
  expectedRevision?: string
  /** Unix seconds captured by the first page; pair with expectedRevision. */
  evaluatedAt?: number
  factsLimit?: number
  factsOffset?: number
  episodesLimit?: number
  episodesOffset?: number
  status?: MemoryStatusFilter
  /** 对事实的文本搜索；仅当 includeSensitive=true 时才搜索原始 episode。 */
  query?: string
  /** query 的别名，与 HTTP 参数名一致。 */
  q?: string
  /** 默认返回可安全分享的列表；仅在需要所有者可见的原始 episode/画像时传 true。 */
  includeSensitive?: boolean
  /** 拆分事实集合：'outcomes' = 仅会话结论，'attributes' = 其余全部。
   *  与 `status` 一样，未知值会被服务器忽略。 */
  kind?: MemoryKindFilter
}

export type MemoryKindFilter = 'outcomes' | 'attributes'

/** 分页的记忆视图（GET /v1/memories）；默认可安全分享。 */
export interface MemoryDump {
  user: string
  data_revision: string
  evaluated_at: number
  profile: string
  counts: MemoryCounts
  facts: Fact[]
  episodes: EpisodeView[]
  facts_page: MemoryPage<Fact>
  episodes_page: MemoryPage<EpisodeView>
  next_offsets: {
    facts: number | null
    episodes: number | null
  }
}

export interface MemoryStats {
  user: string
  counts: {
    episodes: number
    episodes_consolidated: number
    extraction_failures?: number
  episodes_pending: number
    episodes_ephemeral: number
    facts_hot: number
    facts_cold: number
    cold_pages_out: number
    cold_pages_in: number
    facts_live: number
    facts_superseded: number
    facts_sensitive: number
    working_live: number
    summaries: number
    entities: number
    relations: number
    graph_orphan_entities: number
    graph_stale_relations: number
    pending_conflicts: number
  }
  time_range: {
    first_event_at: number | null
    first_event_at_h: string | null
    last_event_at: number | null
    last_event_at_h: string | null
    oldest_fact_valid_at: number | null
    oldest_fact_valid_at_h: string | null
    newest_fact_valid_at: number | null
    newest_fact_valid_at_h: string | null
  }
  storage: string
  max_hot_facts: number
  embedder: string
  llm_configured: boolean
  answerer_configured: boolean
  consolidation_backlog: boolean
}

export interface AgentStatus {
  ok: boolean
  user: string
  session_id: string | null
  mode: 'content_free_agent_status'
  focus: Focus
  session: {
    id: string | null
    episodes: number
    extraction_failures?: number
  episodes_pending: number
    working_live: number
  }
  counts: MemoryStats['counts']
  consolidation_backlog: boolean
  storage: string
  embedder: string
  llm_configured: boolean
  recommended_next_actions: string[]
  tools: {
    read_context: string
    write_memory: string
    close_session: string
    inspect_facts: string
    correct_fact: string
    delete_fact: string
    focus: string
  }
}

export interface ProfileResult {
  profile: string
  facts: string[]
}

export interface Focus {
  track: string[]
  mute: string[]
}

export interface Policy {
  extract_instruction: string
  extract_system: string
  summary_system: string
  persona_system: string
}

export interface PolicyResponse {
  policy: Policy
  defaults: Policy
}

export interface GraphNode {
  id: string
  name: string
  type: string
}

export interface GraphEdge {
  source: string
  target: string
  predicate: string
  live: boolean
  fact_id: string
  fact_text: string
  valid_at: number
  valid_at_h: string
  invalid_at: number | null
  invalid_at_h: string | null
  provenance: string[]
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphResponse extends GraphData {
  has_more: boolean
}

export interface FactInput {
  subject?: string
  predicate: string
  object: string
  sensitive?: boolean
  category?: string
  topic?: Topic | null
}

export interface FactPatch {
  subject?: string
  predicate?: string
  object?: string
  sensitive?: boolean
  category?: string
  topic?: Topic | null
  reset_topic?: boolean
}

export interface ImportMessageInput {
  role?: string
  speaker?: string
  content: string
  event_time?: number
}

export interface ImportSessionInput {
  session_id: string
  messages: ImportMessageInput[]
  title?: string
  event_time?: number
}

/** 批量导入：传入已解析的 `sessions`，或传入原始 `data` + `format` 由服务器解析。 */
export interface ImportParams {
  sessions?: ImportSessionInput[]
  data?: unknown
  /** chatgpt | messages | records | jsonl | transcript | auto（默认） */
  format?: string
  consolidate?: boolean
  summarize?: boolean
}

export interface ImportResult {
  summarization?: Record<string, SummarizationResult>
  ok: boolean
  sessions: number
  episodes: number
  facts_added: number
  summaries: number
}

export interface MemoryExportFact extends TopicMetadata {
  id: string
  subject: string
  predicate: string
  object: string
  text: string
  source: FactSource
  status: FactStatus
  category: string
  sensitive: boolean
  salience: number
  confidence: number
  valid_at: number
  valid_at_h: string
  invalid_at: number | null
  invalid_at_h: string | null
  created_at: number
  expired_at: number | null
  supersedes: string | null
  provenance: string[]
}

export interface MemoryExportEpisode {
  id: string
  session_id: string
  speaker: string
  event_time: number
  date: string
  content: string
  summary: string
}

export interface MemoryExport {
  user: string
  include_sensitive: boolean
  redacted_sensitive: boolean
  profile: string
  focus: Focus
  facts: MemoryExportFact[]
  episodes: MemoryExportEpisode[]
  graph: GraphData
  redaction_note?: string
}

export interface ForgetOptions {
  /** 必须为 true；清空命名空间不可逆。 */
  confirm: true
}

// --- OpenAI 兼容 chat（POST /v1/chat/completions）---------------------------

export type ChatRole = 'system' | 'user' | 'assistant' | 'tool'

export interface ChatMessage {
  role: ChatRole
  /** 字符串，或 OpenAI 多模态 content parts */
  content: string | Array<Record<string, unknown>>
}

/** 忆痕扩展，用于按请求控制记忆层。 */
export interface MemoryControls {
  /** 回答前检索并注入相关记忆（默认 true） */
  recall?: boolean
  /** 回答后记住用户这一轮输入（默认 true） */
  remember?: boolean
  /** 用于检索和后台写入的会话/线程 id */
  session_id?: string
  /** auto | long | working；控制后台写入的路由方式 */
  scope?: 'auto' | 'long' | 'working'
  /** 为补充细节而包含的完整历史对话数量（默认 6） */
  n_chunks?: number
  /** 用于按时间点查看记忆的 epoch 秒数 */
  as_of?: number
  /** 从注入的记忆中省略标记为敏感的事实 */
  redact_sensitive?: boolean
}

export interface ChatCompletionCreateParams {
  messages: ChatMessage[]
  model?: string
  stream?: boolean
  memory?: MemoryControls
  temperature?: number
  max_tokens?: number
  [key: string]: unknown
}

export interface ChatChoice {
  index: number
  message: { role: 'assistant'; content: string }
  finish_reason: string
}

export interface ChatUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface MnemoTraceChatMeta {
  recalled: boolean
  memory_tokens_est: number
  session_id?: string | null
  as_of: number | null
  redacted_sensitive: boolean
  remembered: boolean
  remember_queued?: boolean
  remember_scope?: string
}

export interface ChatCompletion {
  id: string
  object: 'chat.completion'
  created: number
  model: string
  choices: ChatChoice[]
  usage: ChatUsage
  /** 忆痕扩展：本次请求中记忆层做了什么。 */
  mnemotrace: MnemoTraceChatMeta
}

export interface ChatCompletionChunk {
  id: string
  object: 'chat.completion.chunk'
  created: number
  model: string
  choices: Array<{
    index: number
    delta: { role?: string; content?: string }
    finish_reason: string | null
  }>
}

export interface OkMessage {
  ok: boolean
  message?: string
}
