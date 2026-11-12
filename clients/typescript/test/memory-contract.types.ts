import type { GraphResponse, MemoryDump, MemoryListOptions, OutcomeKind, Category, ProfileSection } from '../src/types'
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

client.setFactSensitive('synthetic-fact', true, space).then(result => {
  const sensitive: boolean = result.sensitive
  return sensitive
})
// @ts-expect-error sensitivity accepts a boolean
client.setFactSensitive('synthetic-fact', 'true', space)
// @ts-expect-error facts cannot be edited directly
client.updateFact('synthetic-fact', { text: 'Synthetic' }, space)

// @ts-expect-error space must be explicit
client.remember('Synthetic input', {sessionId: 's'})
