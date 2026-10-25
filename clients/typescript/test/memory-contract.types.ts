import type { GraphResponse, MemoryDump, MemoryListOptions, OutcomeKind } from '../src/types'
import type { MnemoTraceClient } from '../src/client'

declare const client: MnemoTraceClient
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
