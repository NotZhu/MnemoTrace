// 类型与 MnemoTrace 记忆服务器（mnemotrace/server/app.py）保持镜像。需与 API 同步更新。
import type { Category, ProfileSection } from './generated/memory-contract'
export type { Category, ProfileSection } from './generated/memory-contract'

export type FactStatus = 'live' | 'superseded'
export type FactSource = 'extracted' | 'user'

export type CategoriesSource = 'unset' | 'llm' | 'user'
export interface FactMetadata {
  categories: Category[]
  categories_source: CategoriesSource
}

export interface MemoryFact extends FactMetadata {
  relation_id: string
  subject_id: string | null
  object_id: string | null
  subject_type: string | null
  object_type: string | null
  withdrawn_at: number | null
  source_roles?: ('user' | 'assistant')[]
  id: string
  text: string
  subject: string
  predicate: string
  object: string
  valid_at: string // 服务器生成的人类可读时间戳："YYYY-MM-DD HH:MM:SS"（北京时间，UTC+8）
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  supersedes: string[]
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
  text_older: string
  text_newer: string
  older_version: number
  newer_version: number
  reason: string
}


export interface MemoryEpisode {
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
  digests: number
  entities: number
  relations: number
  graph_orphan_entities: number
  graph_stale_relations: number
  pending_conflicts: number
}

/** 由服务器根据带来源标记的会话记录推导的摄入统计。 */
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
    set_fact_sensitive: string
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
  digests: number
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
  counts: MemoryCounts
  facts: MemoryFact[]
  fact_categories?: Category[]
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
  valid_at: string
  invalid_at: string | null
  status: FactStatus
  source: FactSource
  // 脱敏占位条目不返回分类和对象类型。
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
  type: string | null
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
  context_sections: { kind: string; text: string; space_id: string }[]
  status: "answered" | "evidence_only" | "no_evidence"
  answer_status?: "answered" | "model_not_configured" | "model_failed" | "no_evidence" | "empty"
  tokens_est: number // 精简检索上下文
  full_tokens?: number // 完整历史（full-context 基线）
  answer?: string // agent 基于这份记忆给出的答案（LLM 读取精简上下文）
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
  digest_system: string
  profile_system: string
}

export interface PolicyResponse {
  policy: Policy // 用户覆盖项（"" = 使用默认值）
  defaults: Policy // 内置提示词
}

// --- L2 结构化画像（feature ③）---

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

export interface AuditFinding {
  kind: 'missing_edge' | 'edge_mismatch' | 'orphan_entity'
  why: string
  action: string
  fact_id?: string
  edge_id?: string
  entity_id?: string
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
  kind: 'extract' | 'digest' | 'reembed' | 'graph' | 'profile' | 'conflicts' | 'outcomes'
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
