import type { GraphResponse, MemoryDump, MemoryListOptions, OutcomeKind, Category, CategoriesSource, ObjectKind, ProfileSection } from '../src/types'
import { CATEGORIES, PROFILE_SECTIONS } from '../src'
import type { MnemoTraceClient } from '../src/client'

declare const client: MnemoTraceClient
const space = {spaceId: 'synthetic-space'}
const category: Category = CATEGORIES[0]
const profileSection: ProfileSection = PROFILE_SECTIONS[0]
// @ts-expect-error generated profile names exclude unsupported sections
const invalidProfileSection: ProfileSection = 'unsupported'
client.profile(space).then(result => {
  const count: number = result.pending_fact_count
  const text: string | undefined = result.profile.communication_preferences[0]?.text
  return [count, text]
})
client.refreshProfile(space).then(result => {
  const operationId: string = result.operation_id
  return operationId
})
client.rememberMessages([{message_id: 'm1', role: 'assistant', content: 'Synthetic', event_time: null}], {...space, sessionId: 's'})
// @ts-expect-error dialogue requires a stable identity
client.rememberMessages([{role: 'user', content: 'Synthetic'}], {...space, sessionId: 's'})
// @ts-expect-error tool output is not a complete user/assistant dialogue message
client.rememberMessages([{message_id: 'm1', role: 'tool', content: 'Synthetic'}], {...space, sessionId: 's'})
const outcome: OutcomeKind = 'open_question'
const options: MemoryListOptions = { outcomeKind: outcome, expectedRevision: 'synthetic-v1', evaluatedAt: 1000 }
const response: Promise<MemoryDump> = client.memories({...space, ...options})
response.then(dump => {
  const revision: string = dump.data_revision
  const evaluatedAt: number = dump.evaluated_at
  return [revision, evaluatedAt]
})
// Unsupported outcome types are rejected before a request can be assembled.
// @ts-expect-error invalid outcome type
client.memories({ ...space, outcomeKind: 'unsupported' })

const graph: Promise<GraphResponse> = client.graph(space)
graph.then(result => {
  const hasMore: boolean = result.has_more
  return hasMore
})

client.addFact({ predicate: 'prefers', object: 'tea', text: '用户偏好茶。', categories: ['food'], object_kind: 'entity' }, space).then(result => {
  const categories: Category[] = result.categories
  const source: CategoriesSource = result.categories_source
  const kind: ObjectKind = result.object_kind
  return [categories, source, kind]
})
client.updateFact('synthetic-fact', { categories: [] }, space)
// @ts-expect-error unsupported label
client.updateFact('synthetic-fact', { categories: ['音乐'] }, space)
// @ts-expect-error callers cannot choose classification source
client.addFact({ predicate: 'prefers', object: 'tea', text: '用户偏好茶。', categories_source: 'llm' }, space)

// @ts-expect-error manual writes require a complete sentence
client.addFact({ predicate: 'prefers', object: 'tea' }, space)
// @ts-expect-error content edits require the whole assertion
client.updateFact('synthetic-fact', { object: 'coffee' }, space)
// @ts-expect-error a sentence cannot be edited without structured fields
client.updateFact('synthetic-fact', { text: '用户偏好咖啡。' }, space)
client.updateFact('synthetic-fact', { subject: 'user', predicate: 'prefers', object: 'coffee', text: '用户偏好咖啡。' }, space)

// @ts-expect-error space must be explicit
client.remember('Synthetic input', {sessionId: 's'})
