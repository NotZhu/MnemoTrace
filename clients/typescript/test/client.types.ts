import { MnemoTraceClient, OperationWaitTimeout } from '../src/index'
import type { OperationHandle, OperationResponse, RecallRequest, TerminalOperation } from '../src/index'
import type { operations } from '../src/generated/openapi'

const client = new MnemoTraceClient({ baseUrl: 'http://127.0.0.1:8100', apiKey: 'synthetic-key' })
const request: RecallRequest = { query: '默认导出格式' }

async function checkTypes(spaceId: string): Promise<TerminalOperation> {
  const session = await client.createSession(spaceId)
  const { handle, operation } = await client.remember(
    spaceId, session.session_id, [{ role: 'user', content: '默认导出 CSV。' }],
    { idempotencyKey: 'synthetic-write-1' },
  )
  const saved: OperationHandle = handle
  const operationQuery = {
    status: 'succeeded',
    session_id: session.session_id,
    limit: 10,
  } satisfies NonNullable<operations['listOperations']['parameters']['query']>
  const operationsPage = await client.listOperations(spaceId, operationQuery)
  if (operationsPage.next_cursor) {
    await client.listOperations(spaceId, { ...operationQuery, cursor: operationsPage.next_cursor })
  }
  const auditQuery = {
    include_sensitive: false,
  } satisfies NonNullable<operations['listOperationFactChanges']['parameters']['query']>
  const audit = await client.listOperationFactChanges(saved, auditQuery)
  const factId = audit.items[0]?.fact_id
  if (factId) await client.listFactChanges(spaceId, factId, auditQuery)
  await client.listSessionMessages(spaceId, session.session_id, { limit: 10 })
  const queued: OperationResponse = operation
  if (queued.status === 'succeeded' && queued.message_ids?.[0]) {
    await client.getMessage(spaceId, queued.message_ids[0])
  }
  await client.recall(spaceId, { ...request, rerank: false })
  try {
    return await client.waitOperation(saved)
  } catch (error) {
    if (error instanceof OperationWaitTimeout) await client.getOperation(error.handle)
    throw error
  }
}

void checkTypes
