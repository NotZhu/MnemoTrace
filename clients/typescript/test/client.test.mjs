import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import { MnemoTraceClient, MnemoTraceError, OperationWaitTimeout } from 'mnemotrace-memory'

const BASE = 'https://synthetic.invalid/proxy'
const SPACE = '/v1/spaces/space-1'
const SESSION = SPACE + '/sessions/session-1'
const OPERATION = SPACE + '/operations/operation-1'
const HANDLE = { spaceId: 'space-1', operationId: 'operation-1' }
const KEY = { idempotencyKey: 'synthetic-write-1' }
const TIME = '2026-01-01T00:00:00Z'

function operation(status, fields = {}) {
  return {
    operation_id: 'operation-1',
    session_id: 'session-1',
    created_at: TIME,
    updated_at: TIME,
    status,
    ...fields,
  }
}

function json(body, status = 200, headers) {
  return Response.json(body, { status, headers })
}

function accepted(value, location = OPERATION) {
  return json(value, 202, { Location: location })
}

function problem(code, status) {
  return json({ code, request_id: 'request-1' }, status)
}

function client(fetch) {
  return new MnemoTraceClient({ baseUrl: BASE + '/', apiKey: 'synthetic-key', fetch })
}

test('publishes an equivalent CommonJS entry', () => {
  const require = createRequire(import.meta.url)
  assert.equal(typeof require('mnemotrace-memory').MnemoTraceClient, 'function')
})

test('replays lost submissions and reads the original message after recall', async () => {
  const requests = []
  const messageId = '~YS9i'
  const success = operation('succeeded', { message_ids: [messageId], fact_change_count: 1 })
  const sdk = client(async (url, init) => {
    const path = url.slice(BASE.length)
    assert.equal(url, BASE + path)
    assert.equal(init.headers.get('Authorization'), 'Bearer synthetic-key')
    assert.equal(init.redirect, 'error')
    requests.push({ path, method: init.method ?? 'GET', body: init.body, headers: init.headers })
    switch (path) {
      case SPACE + '/sessions':
        return json({ session_id: 'session-1', created_at: TIME }, 201)
      case SESSION + '/remember':
        if (requests.length === 2) throw new TypeError('synthetic response was lost')
        return accepted(operation('queued'))
      case OPERATION:
        return json(success)
      case SPACE + '/recall':
        return json({
          evidence: [{
            fact_id: 'fact-1',
            text: '默认导出 CSV。',
            valid_at: TIME,
            invalid_at: null,
            out_of_range_fields: [],
            source_message_ids: [messageId],
            relation: null,
          }],
          degraded_reason: null,
          rerank_applied: false,
        })
      case SPACE + '/messages/' + messageId:
        return json({
          message_id: messageId,
          session_id: 'session-1',
          role: 'user',
          content: '默认导出 CSV。',
          event_time: TIME,
          recorded_at: TIME,
          out_of_range_fields: [],
        })
      default:
        assert.fail('意外的 SDK 请求')
    }
  })
  const { session_id } = await sdk.createSession(HANDLE.spaceId)
  assert.deepEqual(await sdk.rememberAndWait(HANDLE.spaceId, session_id, '默认导出 CSV。', KEY), success)
  const recall = await sdk.recall(HANDLE.spaceId, { query: '导出', rerank: false, track_usage: false })
  const message = await sdk.getMessage(HANDLE.spaceId, recall.evidence[0].source_message_ids[0])
  assert.equal(message.content, '默认导出 CSV。')
  assert.deepEqual(requests.map(({ method, path }) => [method, path]), [
    ['POST', SPACE + '/sessions'],
    ['POST', SESSION + '/remember'],
    ['POST', SESSION + '/remember'],
    ['GET', OPERATION],
    ['POST', SPACE + '/recall'],
    ['GET', SPACE + '/messages/' + messageId],
  ])
  assert.deepEqual(JSON.parse(requests[1].body), { messages: [{ role: 'user', content: message.content }] })
  assert.equal(requests[1].body, requests[2].body)
  for (const submission of requests.slice(1, 3)) {
    assert.equal(submission.headers.get('Idempotency-Key'), KEY.idempotencyKey)
    assert.equal(submission.headers.get('Content-Type'), 'application/json')
  }
  assert.deepEqual(JSON.parse(requests[4].body), { query: '导出', rerank: false, track_usage: false })
})

test('honors both retryable server errors and stops after the third submission', async () => {
  let calls = 0
  const responseCodes = ['remember_queue_full', 'commit_outcome_unknown', 'commit_outcome_unknown']
  const sdk = client(async (_url, init) => {
    assert.equal(init.headers.get('Idempotency-Key'), KEY.idempotencyKey)
    return problem(responseCodes[calls++], 503)
  })
  await assert.rejects(sdk.remember(HANDLE.spaceId, 'session-1', '合成原文', KEY), {
    code: 'submit_outcome_unknown',
    status: 503,
    requestId: 'request-1',
  })
  assert.equal(calls, 3)
})

test('preserves an unknown submission outcome when replay is rejected', async () => {
  const submissions = []
  const sdk = client(async (_url, init) => {
    submissions.push({ key: init.headers.get('Idempotency-Key'), body: init.body })
    if (submissions.length === 1) throw new TypeError('synthetic response was lost')
    return problem('database_unavailable', 503)
  })
  await assert.rejects(sdk.remember(HANDLE.spaceId, 'session-1', '合成原文', KEY), error => {
    assert.ok(error instanceof MnemoTraceError)
    assert.equal(error.code, 'submit_outcome_unknown')
    assert.equal(error.status, 503)
    assert.equal(error.requestId, 'request-1')
    assert.equal(error.cause?.code, 'database_unavailable')
    return true
  })
  assert.equal(submissions.length, 2)
  assert.equal(submissions[0].key, KEY.idempotencyKey)
  assert.deepEqual(submissions[1], submissions[0])
})

test('surfaces idempotency conflicts without resubmitting', async () => {
  let calls = 0
  const sdk = client(async () => {
    calls++
    return problem('idempotency_conflict', 409)
  })
  await assert.rejects(sdk.remember(HANDLE.spaceId, 'session-1', '合成原文', KEY), error => {
    assert.ok(error instanceof MnemoTraceError)
    assert.equal(error.code, 'idempotency_conflict')
    assert.equal(error.status, 409)
    assert.equal(error.requestId, 'request-1')
    return true
  })
  assert.equal(calls, 1)
})

test('invalid Location fails the submission without starting a poll', async () => {
  let calls = 0
  const sdk = client(async () => {
    calls++
    return accepted(operation('queued'), 'https://other.invalid' + OPERATION)
  })
  await assert.rejects(sdk.rememberAndWait(
    HANDLE.spaceId, 'session-1', '合成原文', { ...KEY, timeoutMs: 30 },
  ), { code: 'invalid_response' })
  assert.equal(calls, 1)
})

test('returns a terminal replay without polling', async () => {
  const failure = operation('failed', { error: { code: 'inference_failed', stage: 'embedding' } })
  const sdk = client(async (url, init) => {
    assert.equal(url, BASE + SESSION + '/remember')
    assert.equal(init.method, 'POST')
    assert.deepEqual(JSON.parse(init.body), { messages: [{ role: 'assistant', content: '合成回复' }] })
    return accepted(failure)
  })
  assert.deepEqual(await sdk.rememberAndWait(
    HANDLE.spaceId, 'session-1', [{ role: 'assistant', content: '合成回复' }], KEY,
  ), failure)
})

test('rejects an invalid wait budget before submitting', async () => {
  let calls = 0
  const sdk = client(async () => {
    calls++
    return accepted(operation('queued'))
  })
  await assert.rejects(sdk.rememberAndWait(
    HANDLE.spaceId, 'session-1', '合成原文', { ...KEY, timeoutMs: 0 },
  ), RangeError)
  assert.equal(calls, 0)
})

test('waits for next_retry_at and returns a failed terminal operation', async () => {
  let calls = 0
  let retryAt
  const failure = operation('failed', { error: { code: 'operation_timeout', stage: 'publish' } })
  const sdk = client(async () => {
    if (++calls === 1) {
      retryAt = Date.now() + 40
      return json(operation('queued', { next_retry_at: new Date(retryAt).toISOString() }))
    }
    assert.ok(Date.now() >= retryAt)
    return json(failure)
  })
  assert.deepEqual(await sdk.waitOperation(HANDLE, { timeoutMs: 500 }), failure)
  assert.equal(calls, 2)
})

test('wait timeout carries a handle that can resume without resubmission', async () => {
  let calls = 0
  const sdk = client(async (url, { signal }) => {
    assert.equal(url, BASE + OPERATION)
    if (++calls === 1) {
      return new Promise((_, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true })
      })
    }
    return json(operation('succeeded', { message_ids: ['message-1'], fact_change_count: 0 }))
  })
  let handle
  await assert.rejects(sdk.waitOperation(HANDLE, { timeoutMs: 30 }), error => {
    assert.ok(error instanceof OperationWaitTimeout)
    handle = JSON.parse(JSON.stringify(error.handle))
    assert.deepEqual(handle, HANDLE)
    return true
  })
  assert.equal((await sdk.getOperation(handle)).status, 'succeeded')
  assert.equal(calls, 2)
})

for (const slowStage of ['fetch', 'body']) {
  test(`wait deadline applies to a ${slowStage} that ignores cancellation`, async () => {
    let signal
    const slowResponse = async () => {
      await new Promise(resolve => setTimeout(resolve, 80))
      return json(operation('succeeded'))
    }
    const sdk = client(async (_url, init) => {
      signal = init.signal
      if (slowStage === 'fetch') return slowResponse()
      return {
        ok: true,
        async json() { return (await slowResponse()).json() },
      }
    })
    await assert.rejects(sdk.waitOperation(HANDLE, { timeoutMs: 25 }), error => {
      assert.ok(error instanceof OperationWaitTimeout)
      assert.deepEqual(error.handle, HANDLE)
      return true
    })
    assert.equal(signal.aborted, true)
  })
}

test('wait budget takes precedence over a future retry time', async () => {
  let calls = 0
  const sdk = client(async () => {
    calls++
    const retryAt = new Date(Date.now() + 60_000).toISOString()
    return json(operation('queued', { next_retry_at: retryAt }))
  })
  await assert.rejects(sdk.waitOperation(HANDLE, { timeoutMs: 30 }), OperationWaitTimeout)
  assert.equal(calls, 1)
})

test('pages operations and follows audit evidence back to a session message', async () => {
  const auditItem = {
    fact_id: '~b2xkL2ZhY3Q',
    changed_at: TIME,
    before: { valid_at: null, invalid_at: null },
    after: { valid_at: null, invalid_at: TIME },
    cutoff_triggers: [{ fact_id: 'cause', valid_end: TIME, source_message_ids: ['~YS9i'] }],
    out_of_range_fields: [],
  }
  const message = {
    message_id: '~YS9i', session_id: 'session-1', content: '以后使用 XLSX。',
    role: 'user', event_time: TIME, recorded_at: TIME, out_of_range_fields: [],
  }
  const continuation = 'abc_-'
  const requests = []
  const sdk = client(async (url, init) => {
    assert.equal(init.method ?? 'GET', 'GET')
    assert.equal(init.headers.get('Authorization'), 'Bearer synthetic-key')
    const target = new URL(url)
    const path = target.pathname.slice('/proxy'.length)
    requests.push([path, Object.fromEntries(target.searchParams)])
    switch (path) {
      case SPACE + '/operations':
        return json(target.searchParams.has('cursor')
          ? { items: [], next_cursor: null }
          : { items: [operation('succeeded')], next_cursor: continuation })
      case OPERATION + '/fact-changes':
        return json({ status: 'succeeded', items: [auditItem], next_cursor: null })
      case SPACE + '/facts/~b2xkL2ZhY3Q/changes':
        return json({ items: [auditItem], next_cursor: null })
      case SESSION + '/messages':
        return json({ items: [message], next_cursor: null })
      case SPACE + '/messages/~YS9i':
        return json(message)
      default:
        assert.fail('意外的 SDK 请求')
    }
  })

  const options = { limit: 1, status: 'succeeded', session_id: 'session-1' }
  const listed = await sdk.listOperations(HANDLE.spaceId, options)
  const next = await sdk.listOperations(HANDLE.spaceId, { ...options, cursor: listed.next_cursor })
  assert.deepEqual(next, { items: [], next_cursor: null })
  const handle = { spaceId: HANDLE.spaceId, operationId: listed.items[0].operation_id }
  const audit = await sdk.listOperationFactChanges(handle, { include_sensitive: false })
  assert.equal(audit.status, 'succeeded')
  const changes = await sdk.listFactChanges(
    handle.spaceId, audit.items[0].fact_id, { include_sensitive: true, limit: 2 },
  )
  const [sourceId] = changes.items[0].cutoff_triggers[0].source_message_ids
  const messages = await sdk.listSessionMessages(handle.spaceId, listed.items[0].session_id, { limit: 1 })
  assert.deepEqual(messages.items, [await sdk.getMessage(handle.spaceId, sourceId)])
  assert.deepEqual(requests, [
    [SPACE + '/operations', { ...options, limit: '1' }],
    [SPACE + '/operations', { ...options, limit: '1', cursor: continuation }],
    [OPERATION + '/fact-changes', { include_sensitive: 'false' }],
    [SPACE + '/facts/~b2xkL2ZhY3Q/changes', { include_sensitive: 'true', limit: '2' }],
    [SESSION + '/messages', { limit: '1' }],
    [SPACE + '/messages/~YS9i', {}],
  ])
})
