// 类型与 MnemoTrace 记忆服务器（mnemotrace/server/app.py）保持镜像。需与 API 同步更新。

export type FactStatus = 'live' | 'superseded'
export type FactSource = 'extracted' | 'user'

export type Topic = 'food' | 'travel' | 'music' | 'film_tv' | 'sports' | 'reading' | 'hobbies'
  | 'environment' | 'health' | 'work' | 'learning' | 'technology' | 'finance' | 'relationships' | 'unknown'
export type TopicSource = 'unset' | 'rule' | 'llm' | 'user'
export interface TopicMetadata {
  topic: Topic
  topic_source: TopicSource
}

export interface MemoryFact extends TopicMetadata {
  assertion_kind?: 'unspecified' | 'user_statement' | 'assistant_suggestion' | 'user_confirmation' | 'assistant_report'
  source_roles?: ('user' | 'assistant')[]
  id: string
  text: string
  display: string // 本地化渲染结果（中文记录的事实显示为中文）；无则回退为 text
  subject: string
  predicate: string
  object: string
  valid_at: string // 服务器生成的人类可读时间戳："YYYY-MM-DD HH:MM:SS"（北京时间，UTC+8）
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  supersedes: string | null
  category: string
  sensitive: boolean
  salience: number
  provenance: string[]
  // 结论事实把推理依据内嵌在 `text` 中（"statement （依据：why）"）；服务器会把它
  // 拆分到这里，让 journal 行能把主张和依据分行展示。其余情况为 ''。
  why: string
}

export interface Conflict {
  id: string
  older: string
  newer: string
  older_text: string
  newer_text: string
  reason: string
}

export interface WorkingItem {
  id: string
  content: string
  kind: string
  session_id: string
  created: string
  expires_at: string | null
}

export interface MemoryEpisode {
  id: string
  date: string
  session: string
  role: string
  mode: 'live' | 'historical' | null
  coverage: { partial: boolean } | null
  content: string
  summary: string
}

export interface MemoryCounts {
  episodes: number
  facts_live: number
  facts_superseded: number
  summaries: number
  facts_outcomes: number
}

export interface MemoryStatsCounts {
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

/** 由服务器根据 agent 会话 episode 推导；告诉控制台记忆是否在被 `mnemotrace-watch`
 *  持续喂养（无人喂养的记忆会一直为空）。 */
export interface FeedStatus {
  last_fed_at: number | null
  last_fed_at_h: string | null
  sessions: number
  conclusions: number
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
    working_live: number
  }
  counts: MemoryStatsCounts
  consolidation_backlog: boolean
  storage: string
  embedder: string
  llm_configured: boolean
  feed?: FeedStatus
  recommended_next_actions: string[]
  tools: {
    read_context: string
    write_memory: string
    flush_processing: string
    inspect_facts: string
    correct_fact: string
    delete_fact: string
    focus: string
  }
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

export interface SessionsIndex {
  ok: boolean
  space_id: string
  sessions: SessionIndexItem[]
  page: Page<SessionIndexItem>
  next_offset: number | null
}

export interface MemoryDump {
  data_revision: string
  evaluated_at: number
  space_id: string
  profile: string
  counts: MemoryCounts
  facts: MemoryFact[]
  fact_categories?: string[]
  episodes: MemoryEpisode[]
  facts_page?: Page<MemoryFact>
  episodes_page?: Page<MemoryEpisode>
  next_offsets?: {
    facts: number | null
    episodes: number | null
  }
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
  // 脱敏占位条目不返回主题元数据。
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
  space_id: string
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
  facts_page: Page<SessionReportFact>
}

export interface Page<T> {
  items: T[]
  total: number
  offset: number
  limit: number | null
  has_more: boolean
  next_offset: number | null
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
  fact_id?: string
  fact_text?: string
  valid_at_h?: string
  invalid_at_h?: string | null
  provenance?: string[]
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphResponse extends GraphData {
  has_more: boolean
}

export interface Focus {
  track: string[]
  mute: string[]
}

export type RememberScope = 'auto' | 'long' | 'working'

export interface ExtractionFailure {
  code: 'llm_not_configured' | 'processing_incomplete' | 'extraction_failed' | 'extraction_invalid_response' | 'extraction_truncated'
  message: string
  stored_raw: boolean
  episode_ids: string[]
}

export interface MemorySpace {
  space_id: string
  name: string
  status: 'active'
}

export interface ProcessingResult {
  ok: boolean
  status: 'completed' | 'failed' | 'timed_out'
  operation_ids: string[]
  operations: OperationResult[]
}

export interface RememberResult extends Omit<ProcessingResult, 'status'> {
  status: ProcessingResult['status'] | 'accepted' | 'stored'
  source_ids: string[]
}

export interface RecallResult {
  context: string
  tokens_est: number // 精简检索上下文
  full_tokens?: number // 完整历史（full-context 基线）
  answer?: string // agent 基于这份记忆给出的答案（LLM 读取精简上下文）
}

export interface FactWrite {
  subject?: string
  predicate: string
  object: string
  sensitive?: boolean
  category?: string
  topic?: Topic | null
}

export interface FactEdit {
  statement?: string
  subject?: string
  predicate?: string
  object?: string
  sensitive?: boolean
  category?: string
  topic?: Topic | null
  reset_topic?: boolean
}

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

export interface Policy {
  extract_instruction: string
  extract_system: string
  summary_system: string
  persona_system: string
}

export interface PolicyResponse {
  policy: Policy // 用户覆盖项（"" = 使用默认值）
  defaults: Policy // 内置提示词
}

// --- L2 结构化画像（feature ③）---
export interface Evidence {
  kind: 'user' | 'mentions' | 'reinforced'
  source_count: number // 不同来源 Episode ID 的数量，与手动设定和访问强化状态无关。
  reinforcement_count: number // 事实的 access_count；包括手动设定和多来源事实。
}

export interface ProfileBasic {
  field: string
  label: string
  value: string
  evidence: Evidence
  source: FactSource
  fact_id: string
}

export interface ProfileItem extends TopicMetadata {
  item: string
  polarity: 'like' | 'dislike'
  category: string
  evidence: Evidence
  source: FactSource
  fact_id: string
  subject: string
  predicate: string
  object: string
}

export interface ProfileHabit {
  text: string
  evidence: Evidence
  fact_id: string
}

export interface StructuredProfile {
  basic: ProfileBasic[]
  preferences: Record<string, ProfileItem[]>
  habits: ProfileHabit[]
  tentative: ProfileItem[]
  counts: { basic: number; preferences: number; tentative: number; habits: number }
}

export interface AuditFinding {
  kind:
    | 'machine_token'
    | 'empty_value'
    | 'unreduced_claim'
    | 'orphan_entity'
    | 'fragment'
    | 'code_artifact'
    | 'dangling_subject'
    | 'slot_overflow'
    | 'embedder_blind'
  why: string
  action: string
  fact_id?: string
  text?: string
  display?: string
  subject?: string
  predicate?: string
  object?: string
  source?: FactSource
  valid_at_h?: string
  entity?: string
  // slot_overflow 是一种分组结果：N 条事实挤进同一个单值槽位只占一行，因此它
  // 不携带单个 fact_id，而是携带数量、槽位的人类可读标签和几条样例。
  label?: string
  count?: number
  samples?: Array<{ fact_id: string; text: string }>
  embedder?: string
  ratio?: number
  migrate?: string
}

export interface AuditReport {
  space_id: string
  checked: { facts: number; entities: number }
  total_findings: number
  by_kind: Record<string, number>
  findings: AuditFinding[]
  truncated: boolean
}

export interface OperationResult {
  operation_id: string
  kind: 'extract' | 'summary' | 'reembed' | 'graph' | 'conflicts' | 'outcomes'
  state: 'pending' | 'running' | 'retry_wait' | 'failed' | 'completed' | 'cancelled'
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
