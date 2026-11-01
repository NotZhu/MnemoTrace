import type { GraphResponse, MemoryDump, MemoryListOptions, OutcomeKind, Topic, TopicSource } from '../src/types'
import type { MnemoTraceClient } from '../src/client'

declare const client: MnemoTraceClient
client.rememberMessages([{message_id: 'm1', role: 'assistant', content: 'Synthetic', event_time: null}], {sessionId: 's'})
// @ts-expect-error dialogue requires a stable identity
client.rememberMessages([{role: 'user', content: 'Synthetic'}], {sessionId: 's'})
// @ts-expect-error tool output is not a complete user/assistant dialogue message
client.rememberMessages([{message_id: 'm1', role: 'tool', content: 'Synthetic'}], {sessionId: 's'})
const outcome: OutcomeKind = 'open_question'
const options: MemoryListOptions = { outcomeKind: outcome, expectedRevision: 'synthetic-v1', evaluatedAt: 1000 }
const response: Promise<MemoryDump> = client.memories(options)
response.then(dump => {
  const revision: string = dump.data_revision
  const evaluatedAt: number = dump.evaluated_at
  return [revision, evaluatedAt]
})
// Unsupported outcome types are rejected before a request can be assembled.
// @ts-expect-error invalid outcome type
client.memories({ outcomeKind: 'unsupported' })

const graph: Promise<GraphResponse> = client.graph()
graph.then(result => {
  const hasMore: boolean = result.has_more
  return hasMore
})

client.addFact({ predicate: 'prefers', object: 'tea', topic: 'food' }).then(result => {
  const topic: Topic = result.topic
  const source: TopicSource = result.topic_source
  return [topic, source]
})
client.updateFact('synthetic-fact', { reset_topic: true })
client.updateFact('synthetic-fact', { topic: null })
// @ts-expect-error unsupported topic
client.updateFact('synthetic-fact', { topic: '音乐' })
// @ts-expect-error callers cannot choose the topic source
client.addFact({ predicate: 'prefers', object: 'tea', topic_source: 'llm' })
