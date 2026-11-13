/**
 * 与 忆痕 HTTP API（mnemotrace/server/app.py）对应的类型定义。
 * 这些就是服务器实际返回的响应结构，因此 SDK 调用具备完整类型。
 */

import type { Category, ProfileSection } from './generated/memory-contract'
export type { Category, ProfileSection } from './generated/memory-contract'
export { CATEGORIES, PROFILE_SECTIONS } from './generated/memory-contract'

export interface Health {
  ok: boolean
  ready: boolean
  storage: 'postgres'
  auth_mode: 'api_keys'
  service?: string
  version?: string
  anonymous_allowed?: false
  embedder?: string
  llm_configured?: boolean
  answerer_configured?: boolean
}

export interface ExtractionFailure {
  code: 'llm_not_configured' | 'processing_incomplete' | 'extraction_failed' | 'extraction_invalid_response' | 'extraction_truncated'
  message: string
  stored_raw: boolean
  episode_ids: string[]
}

export interface OriginalEpisode {
  message_id: string | null
  sequence: number | null
  coverage: {unit: 'unicode_code_points'; ranges: [number, number][]; partial: boolean;
    policy: string; original_length: number; input_hash: string; source: string; strategy_completed: boolean} | null
  id: string
  space_id: string
  agent_id: string | null
  session_id: string
  role: string
  event_time: number | null
  ingested_at: number
  content: string
}

export interface MemorySpace {
  space_id: string
  name: string
  status: 'active'
}

export type ProcessingState = 'pending' | 'running' | 'retry_wait' | 'failed' | 'completed' | 'cancelled'

export interface OperationResult {
  operation_id: string
  kind: 'extract' | 'digest' | 'reembed' | 'graph' | 'profile' | 'conflicts' | 'outcomes'
  state: ProcessingState
  phase: 'queued' | 'generate' | 'embed' | 'commit' | 'compensate' | 'done'
  stored_raw: boolean
  extraction_saved: boolean
  core_committed: boolean
  attempts: number
  next_attempt_at: number | null
  error_code: string | null
  auxiliary_operation_ids: string[]
}

export interface ProcessingStatus {
  operations: OperationResult[]
  next_after_id: string | null
}

export interface ProcessingFlushResult {
  ok: boolean
  status: 'completed' | 'failed' | 'timed_out'
  operation_ids: string[]
  operations: OperationResult[]
}

export interface RememberResult extends Omit<ProcessingFlushResult, 'status'> {
  status: ProcessingFlushResult['status'] | 'accepted' | 'stored'
  source_ids: string[]
}

/** 检索到的精简上下文 — 用于作答的一小段带日期切片（recall）。 */
export interface RecallResult {
  space_id?: string
  space_ids?: string[]
  coverage_by_space: Record<string, string[]>
  limitations: string[]
  context: string
  context_sections: { kind: string; text: string; space_id: string }[]
  status: "answered" | "evidence_only" | "no_evidence"
  answer_status?: "answered" | "model_not_configured" | "model_failed" | "no_evidence" | "empty"
  tokens_est: number
  as_of: number | null
  redacted_sensitive: boolean
  full_tokens?: number
  answer?: string
}

/** 直接给出的事实性答案及支撑事实（search）。 */
export interface SearchResult {
  space_id: string
  limitations: string[]
  answer: string
  status: "answered" | "evidence_only" | "no_evidence"
  fact_ids: string[]
  source_ids: string[]
  sources: { id: string; session_id: string; text: string; representation: "digest" }[]
  facts: string[]
  as_of: number | null
  redacted_sensitive: boolean
}

export interface SessionReportFact {
  id: string
  text: string
  valid_at: string
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  /** Omitted for redacted rows. */
  categories?: Category[]
  categories_source?: CategoriesSource
  sensitive: boolean
  redacted: boolean
  provenance: string[]
  subject?: string
  predicate?: string
  object?: string
}

export interface SessionReport {
  ok: boolean
  space_id: string
  session_id: string
  include_sensitive: boolean
  episodes: number
  episodes_consolidated: number
  extraction_failures?: number
  episodes_pending: number
  facts_added: number
  facts_redacted: number
  facts: SessionReportFact[]
  facts_page: MemoryPage<SessionReportFact>
}

export interface SessionIndexItem {
  id: string
  episodes: number
  episodes_consolidated: number
  extraction_failures?: number
  episodes_pending: number
  facts_added: number
  facts_sensitive: number
  digests: number
  first_event_at: number | null
  first_event_at_h: string | null
  last_event_at: number | null
  last_event_at_h: string | null
}

export interface SessionIndex {
  ok: boolean
  space_id: string
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

export type CategoriesSource = 'unset' | 'llm' | 'user'
export interface FactMetadata {
  categories: Category[]
  categories_source: CategoriesSource
}

export interface Fact extends FactMetadata {
  relation_id: string | null
  subject_id: string | null
  object_id: string | null
  subject_type: string | null
  object_type: string | null
  withdrawn_at: number | null
  retention: 'durable' | 'normal' | 'unknown'
  source_roles?: ('user' | 'assistant')[]
  id: string
  text: string
  subject: string
  predicate: string
  object: string
  /** YYYY-MM-DD（生效起始日） */
  valid_at: string
  /** YYYY-MM-DD，仍有效时为 null */
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  supersedes: string[]
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
  date: string
  session: string
  role: string
  coverage: { partial: boolean } | null
  content: string
  digest: string
}

export interface MemoryCounts {
  episodes: number
  facts_live: number
  facts_superseded: number
  digests: number
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
  /** Match facts that have any of these labels. */
  categories?: Category[]
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
  space_id: string
  data_revision: string
  evaluated_at: number
  counts: MemoryCounts
  facts: Fact[]
  fact_categories: Category[]
  episodes: EpisodeView[]
  facts_page: MemoryPage<Fact>
  episodes_page: MemoryPage<EpisodeView>
  next_offsets: {
    facts: number | null
    episodes: number | null
  }
}

export interface MemoryStats {
  space_id: string
  counts: {
    episodes: number
    episodes_consolidated: number
    episodes_pending: number
    extraction_failures: number
    facts: number
    facts_live: number
    facts_superseded: number
    facts_sensitive: number
    facts_outcomes: number
    digests: number
    entities: number
    relations: number
    pending_conflicts: number
  }
  storage: 'postgres'
  embedder: string
  llm_configured: boolean
  answerer_configured: boolean
  consolidation_backlog: boolean
}

export interface AgentStatus {
  ok: boolean
  space_id: string
  session_id: string | null
  mode: 'content_free_agent_status'
  focus: Focus
  session: {
    id: string | null
    episodes: number
    extraction_failures?: number
    episodes_pending: number
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
    flush_processing: string
    inspect_facts: string
    set_fact_sensitive: string
    delete_fact: string
    focus: string
  }
}


export interface ProfileEntry {
  text: string
  fact_ids: string[]
  fact_count: number
}

export interface ProfileResult {
  profile: Record<ProfileSection, ProfileEntry[]>
  cache_version: number
  updated_at: number | null
  pending_fact_count: number
  needs_refresh: boolean
  operation_id: string | null
  refresh_state: string | null
  refresh_error: string | null
}

export interface Focus {
  track: string[]
  mute: string[]
}

export interface Policy {
  extract_instruction: string
  extract_system: string
  digest_system: string
  profile_system: string
}

export interface PolicyResponse {
  policy: Policy
  defaults: Policy
}

export interface GraphNode {
  id: string
  name: string
  type: string | null
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

export interface ImportMessageInput {
  role?: string
  message_id?: string
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


export interface MemoryExportFact extends FactMetadata {
  relation_id: string | null
  subject_id: string | null
  object_id: string | null
  subject_type: string | null
  object_type: string | null
  withdrawn_at: number | null
  retention: Fact["retention"]
  id: string
  subject: string
  predicate: string
  object: string
  text: string
  source: FactSource
  status: FactStatus
  sensitive: boolean
  salience: number
  confidence: number
  valid_at: number
  valid_at_h: string
  invalid_at: number | null
  invalid_at_h: string | null
  created_at: number
  expired_at: number | null
  supersedes: string[]
  provenance: string[]
}

export interface MemoryExportEpisode {
  id: string
  session_id: string
  role: string
  event_time: number
  date: string
  content: string
  digest: string
}

export interface MemoryExport {
  format_version: 4
  space_id: string
  include_sensitive: boolean
  redacted_sensitive: boolean
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
  space_id: string
  agent_id?: string
  message_id?: string
  /** 回答前检索并注入相关记忆（默认 true） */
  recall?: boolean
  /** 回答后记住用户这一轮输入（默认 true） */
  remember?: boolean
  /** 用于检索和后台写入的会话/线程 id */
  session_id?: string
  /** auto | long；控制后台写入的路由方式 */
  extract?: boolean
  /** 为补充细节而包含的完整历史对话数量（默认 6） */
  n_digests?: number
  /** 用于按时间点查看记忆的 epoch 秒数 */
  as_of?: number
  /** 从注入的记忆中省略标记为敏感的事实 */
  redact_sensitive?: boolean
}

export interface ChatCompletionCreateParams {
  messages: ChatMessage[]
  model?: string
  stream?: boolean
  memory: MemoryControls
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
  space_id: string
  operation_ids?: string[]
  recalled: boolean
  memory_tokens_est: number
  session_id?: string | null
  as_of: number | null
  redacted_sensitive: boolean
  remembered: boolean
  remember_queued?: boolean
  remember_extract?: boolean
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
  mnemotrace?: MnemoTraceChatMeta
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
/** Complete dialogue input. IDs are stable within a space and session. */
export interface DialogueMessage {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  event_time?: number | null
}

export interface MessageReceipt {
  message_id: string
  episode_id: string
  operation_ids: string[]
}

export interface MessagesResult extends RememberResult {
  accepted: number
  messages: MessageReceipt[]
}


export interface StructuredReadOptions {
  state?: 'current' | 'history' | 'all'
  valid_at?: number
  redact_sensitive?: boolean
  limit?: number
}

export interface AggregateFactsOptions extends StructuredReadOptions {
  operation?: 'count' | 'count_distinct'
  subject?: string
  start?: number
  end?: number
  query?: string
}

export interface AggregateFactsResult {
  count: number
  facts: Fact[]
  candidates: { id: string; session_id: string; text: string; event_time: number | null; role: string; representation: string }[]
  limitations: string[]
}

export interface FollowPathResult {
  paths: { endpoint: string; facts: Fact[] }[]
  limitations: string[]
}
