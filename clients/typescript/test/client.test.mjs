import assert from 'node:assert/strict'
import test from 'node:test'

import { MnemoTraceClient, MnemoTraceError } from '../dist/index.js'
import { createRequire } from 'node:module'

const space = {spaceId: 'space-test'}

test('profile reads cached sections and refresh creates a space-scoped operation', async () => {
  const calls = []
  const cache = {profile: {basic: [], relationships: [], interests: [], habits: [], restrictions: [],
    goals: [], communication_preferences: []}, updated_at: null, cache_version: 0,
    pending_fact_count: 1, needs_refresh: true, operation_id: null, refresh_state: null, refresh_error: null}
  const operation = {operation_id: 'profile-op', kind: 'profile', state: 'pending'}
  const client = new MnemoTraceClient({fetch: async (url, init) => {
    calls.push({url: new URL(url), method: init.method, body: init.body && JSON.parse(init.body)})
    return new Response(JSON.stringify(init.method === 'POST' ? operation : cache),
      {status: init.method === 'POST' ? 202 : 200})
  }})
  assert.deepEqual(await client.profile(space), cache)
  assert.deepEqual(await client.refreshProfile(space), operation)
  assert.equal(calls[0].url.pathname, '/v1/profile')
  assert.equal(calls[0].url.searchParams.get('space_id'), 'space-test')
  assert.equal(calls[1].url.pathname, '/v1/profile/refresh')
  assert.deepEqual(calls[1].body, {space_id: 'space-test'})
})

test('history import is absent from the public client', () => {
  assert.equal(MnemoTraceClient.prototype.import, undefined)
})

test('dialogue messages preserve identities, roles, unknown time and batch policy', async () => {
  const calls = []
  const client = new MnemoTraceClient({fetch: async (url, init) => {
    calls.push({url, body: JSON.parse(init.body)})
    return new Response(JSON.stringify({ok: true, queued: true, messages: []}), {status: 202})
  }})
  const messages = [{message_id: 'u1', role: 'user', content: 'Synthetic dialogue'},
    {message_id: 'a1', role: 'assistant', content: 'Synthetic suggestion', event_time: null}]
  await client.rememberMessages(messages, {...space, sessionId: 's'})
  assert.equal(new URL(calls[0].url).pathname, '/v1/messages')
  assert.deepEqual(calls[0].body, {messages, session_id: 's', space_id: 'space-test'})
  await client.rememberMessages(messages, {...space, sessionId: 's', extract: false})
  await client.remember('Synthetic note', {...space, extract: false})
  assert.deepEqual(calls[1].body, {messages, session_id: 's', extract: false, space_id: 'space-test'})
  assert.deepEqual(calls[2].body, {content: 'Synthetic note', extract: false, space_id: 'space-test'})
})

test('buffered writes accept 202 and expose status and explicit retry controls', async () => {
  const calls = []
  const client = new MnemoTraceClient({ fetch: async (url, init) => {
    calls.push({url, body: init.body && JSON.parse(init.body)})
    return new Response(JSON.stringify({ok: true, status: 'accepted'}), {status: calls.length === 1 ? 202 : 200})
  } })
  assert.equal((await client.remember('synthetic', {...space, processing: 'buffered'})).status, 'accepted')
  await client.processingStatus({sessionId: 'synthetic session', spaceId: 'team'})
  await client.flushProcessing({...space, sessionId: 'synthetic session', retryFailed: true, timeout: 0})
  assert.equal(calls[0].body.processing, 'buffered')
  assert.equal(new URL(calls[1].url).searchParams.get('session_id'), 'synthetic session')
  assert.deepEqual(calls[2].body, {session_id: 'synthetic session', timeout: 0, retry_failed: true, space_id: 'space-test'})
})

for (const [status, code, storedRaw] of [
  [503, 'llm_not_configured', false],
  [503, 'processing_incomplete', true],
  [502, 'extraction_failed', true],
  [502, 'extraction_invalid_response', true],
  [502, 'extraction_truncated', true],
]) {
  test(`remember preserves ${code} and raw-save details without retrying`, async () => {
    let calls = 0
    const detail = { code, message: 'Synthetic extraction error', stored_raw: storedRaw,
      episode_ids: storedRaw ? ['synthetic-episode'] : [] }
    const client = new MnemoTraceClient({ fetch: async () => {
      calls += 1
      return new Response(JSON.stringify({ detail }), { status })
    } })
    await assert.rejects(client.remember('Synthetic input', space), error => {
      assert.ok(error instanceof MnemoTraceError)
      assert.equal(error.status, status)
      assert.deepEqual(error.detail, { detail })
      return true
    })
    assert.equal(calls, 1)
  })
}

test('remember reports scheduling without fabricating an extraction result', async () => {
  const result = { ok: true, queued: true, episode_id: 'synthetic-episode' }
  const client = new MnemoTraceClient({ fetch: async () =>
    new Response(JSON.stringify(result), { status: 200 }) })
  assert.deepEqual(await client.remember('Synthetic input', space), result)
})

test('memories sends multiple category filters alongside paging and search', async () => {
  const calls = []
  const client = new MnemoTraceClient({ fetch: async url => {
    calls.push(new URL(url))
    return new Response(JSON.stringify({ facts: [] }), { status: 200 })
  } })
  await client.memories({ ...space, categories: ['food', 'music'], kind: 'outcomes', factsLimit: 1, query: 'synthetic' })
  assert.deepEqual(calls[0].searchParams.getAll('categories'), ['food', 'music'])
  assert.equal(calls[0].searchParams.get('facts_limit'), '1')
  assert.equal(calls[0].searchParams.get('q'), 'synthetic')
  await client.memories(space)
  assert.equal(calls[1].searchParams.has('categories'), false)
})

test('sensitivity changes send only the flag and scope', async () => {
  const calls = []
  const client = new MnemoTraceClient({ fetch: async (url, init) => {
    calls.push([url, init.method, JSON.parse(init.body)])
    return new Response(JSON.stringify({ ok: true, id: 'fact/a', sensitive: false }), { status: 200 })
  } })
  assert.deepEqual(await client.setFactSensitive('fact/a', false, space),
    { ok: true, id: 'fact/a', sensitive: false })
  assert.deepEqual(calls, [[
    'http://localhost:8000/v1/facts/fact%2Fa/sensitivity', 'PATCH', { sensitive: false, space_id: 'space-test' },
  ]])
  assert.equal(client.addFact, undefined)
  assert.equal(client.updateFact, undefined)
})

test('graph exposes completeness metadata and preserves safe default query options',async()=>{
  const calls=[]
  const response={nodes:[],edges:[],has_more:false}
  const client=new MnemoTraceClient({fetch:async url=>{
    calls.push(url)
    return new Response(JSON.stringify(response),{status:200})
  }})
  assert.deepEqual(await client.graph(space),response)
  assert.equal(calls[0],'http://localhost:8000/v1/graph?space_id=space-test')
  await client.graph({...space, asOf:100,includeSensitive:true})
  assert.equal(calls[1],'http://localhost:8000/v1/graph?as_of=100&include_sensitive=true&space_id=space-test')
})

const counts = {
  episodes: 0,
  episodes_consolidated: 0,
  episodes_pending: 0,
  episodes_ephemeral: 0,
  facts_hot: 0,
  facts_cold: 0,
  cold_pages_out: 0,
  cold_pages_in: 0,
  facts_live: 0,
  facts_superseded: 0,
  facts_sensitive: 0,
  digests: 0,
  entities: 0,
  relations: 0,
  graph_orphan_entities: 0,
  graph_stale_relations: 0,
  pending_conflicts: 0,
}

test('ESM and CommonJS expose only the new runtime API', async () => {
  const esm = await import('../dist/index.js')
  const cjs = createRequire(import.meta.url)('../dist/index.cjs')
  for (const sdk of [esm, cjs]) {
    assert.equal(typeof sdk.MnemoTraceClient, 'function')
    assert.equal(typeof sdk.MnemoTraceError, 'function')
    assert.ok(sdk.CATEGORIES.includes('food'))
    assert.ok(sdk.PROFILE_SECTIONS.includes('communication_preferences'))
    assert.equal(Object.hasOwn(sdk, 'EngramClient'), false)
    assert.equal(Object.hasOwn(sdk, 'EngramError'), false)
  }
})

test('chat preserves model IDs and returns the mnemotrace response extension', async () => {
  const calls = []
  const meta = {
    recalled: true,
    memory_tokens_est: 42,
    session_id: 'sdk-demo:chat',
    as_of: null,
    redacted_sensitive: false,
    remembered: false,
  }
  const model = 'deepseek/deepseek-chat'
  const client = new MnemoTraceClient({
    fetch: async (url, init) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({
        id: 'chatcmpl-test',
        object: 'chat.completion',
        created: 0,
        model,
        choices: [{ index: 0, message: { role: 'assistant', content: 'Test reply' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 42, completion_tokens: 2, total_tokens: 44 },
        mnemotrace: meta,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })
  const completion = await client.chat.completions.create({
    model,
    messages: [{ role: 'user', content: 'Test question' }],
    memory: { session_id: meta.session_id, remember: false },
  })
  assert.equal(calls[0].url, 'http://localhost:8000/v1/chat/completions')
  assert.equal(JSON.parse(calls[0].init.body).model, model)
  assert.deepEqual(completion.mnemotrace, meta)
  assert.equal(Object.hasOwn(completion, 'engram'), false)
})

test('request failures use the new error identity and default message', async () => {
  const client = new MnemoTraceClient({
    fetch: async () => new Response('Unavailable', { status: 503 }),
  })
  await assert.rejects(client.health(), (err) => {
    assert.ok(err instanceof MnemoTraceError)
    assert.equal(err.name, 'MnemoTraceError')
    assert.equal(err.status, 503)
    assert.equal(err.message, 'MnemoTrace request failed (503)')
    return true
  })
})

test('agentStatus calls the content-free agent status endpoint', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    return new Response(JSON.stringify({
      ok: true,
      user: 'sdk-user',
      session_id: 'codex:repo/thread 1',
      mode: 'content_free_agent_status',
      focus: { track: [], mute: [] },
      session: {
        id: 'codex:repo/thread 1',
        episodes: 0,
        episodes_pending: 0,
      },
      counts,
      consolidation_backlog: false,
      storage: 'memory',
      embedder: 'lexical',
      llm_configured: false,
      recommended_next_actions: [
        'Call mnemotrace_recall before answering tasks that depend on prior user/project context.',
      ],
      tools: {
        read_context: 'mnemotrace_recall',
        write_memory: 'mnemotrace_remember',
        close_session: 'mnemotrace_close_session',
        inspect_facts: 'mnemotrace_list_facts',
        set_fact_sensitive: 'mnemotrace_set_fact_sensitive',
        delete_fact: 'mnemotrace_delete_fact',
        focus: 'mnemotrace_get_focus / mnemotrace_set_focus',
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test/v1',
    apiKey: 'sk-test',
    fetch,
  })

  const status = await client.agentStatus({ ...space, sessionId: 'codex:repo/thread 1' })

  assert.equal(calls.length, 1)
  assert.equal(
    calls[0].url,
    'http://mnemotrace.test/v1/agent/status?session_id=codex%3Arepo%2Fthread%201&space_id=space-test',
  )
  assert.equal(calls[0].init.method, undefined)
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.equal(calls[0].init.headers['Content-Type'], undefined)
  assert.equal(status.mode, 'content_free_agent_status')
  assert.equal(status.tools.read_context, 'mnemotrace_recall')
})

test('sessionReport calls the session audit endpoint with redaction default', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    return new Response(JSON.stringify({
      ok: true,
      user: 'sdk-user',
      session_id: 'codex:repo/thread 1',
      include_sensitive: false,
      episodes: 1,
      episodes_consolidated: 1,
      episodes_pending: 0,
      facts_added: 1,
      facts_redacted: 0,
      facts: [],
      facts_page: { items: [], total: 1, offset: 100, limit: 10, has_more: false, next_offset: null },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test',
    apiKey: 'sk-test',
    fetch,
  })

  const report = await client.sessionReport('codex:repo/thread 1', { ...space, offset: 100, limit: 10 })

  assert.equal(calls.length, 1)
  assert.equal(
    calls[0].url,
    'http://mnemotrace.test/v1/sessions/report?session_id=codex%3Arepo%2Fthread+1&include_sensitive=false&limit=10&offset=100&space_id=space-test',
  )
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.equal(report.session_id, 'codex:repo/thread 1')
  assert.equal(report.include_sensitive, false)
  assert.equal(report.facts_page.next_offset, null)
})

test('sessions calls the content-free cross-agent session index endpoint', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    return new Response(JSON.stringify({
      ok: true,
      user: 'sdk-user',
      sessions: [
        {
          id: 'claude-code:repo/thread 2',
          episodes: 2,
          episodes_consolidated: 2,
          episodes_pending: 0,
          facts_added: 3,
          facts_sensitive: 1,
          digests: 1,
          first_event_at: 1782500000,
          first_event_at_h: '2026-06-27 10:00:00',
          last_event_at: 1782503600,
          last_event_at_h: '2026-06-27 11:00:00',
        },
      ],
      page: {
        offset: 4,
        limit: 2,
        total: 7,
        next_offset: 6,
        has_more: true,
        items: [],
      },
      next_offset: 6,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test/v1',
    apiKey: 'sk-test',
    fetch,
  })

  const index = await client.sessions({ ...space, limit: 2, offset: 4, query: 'claude-code' })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'http://mnemotrace.test/v1/sessions?limit=2&offset=4&q=claude-code&space_id=space-test')
  assert.equal(calls[0].init.method, undefined)
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.equal(calls[0].init.headers['Content-Type'], undefined)
  assert.equal(index.sessions[0].id, 'claude-code:repo/thread 2')
  assert.equal(index.page.next_offset, 6)
})

test('memories supports paging search and share-safe filtering for owner UIs', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    return new Response(JSON.stringify({
      user: 'sdk-user',
      profile: '',
      counts: {
        episodes: 2,
        facts_live: 3,
        facts_superseded: 1,
        digests: 1,
      },
      facts: [
        {
          id: 'fact-1',
          text: 'user works at Moonshot AI',
          subject: 'user',
          predicate: 'employer',
          object: 'Moonshot AI',
          valid_at: '2026-06-27',
          invalid_at: null,
          status: 'live',
          source: 'user',
          supersedes: null,
          categories: ['professional_details'],
          categories_source: 'user',
          sensitive: false,
          salience: 1,
          provenance: [],
        },
      ],
      episodes: [],
      facts_page: {
        offset: 4,
        limit: 2,
        total: 7,
        next_offset: 6,
        has_more: true,
        items: [],
      },
      episodes_page: {
        offset: 0,
        limit: 0,
        total: 0,
        next_offset: null,
        has_more: false,
        items: [],
      },
      next_offsets: { facts: 6, episodes: null },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test',
    apiKey: 'sk-test',
    fetch,
  })

  const dump = await client.memories({ ...space,
    factsLimit: 2,
    factsOffset: 4,
    episodesLimit: 0,
    status: 'live',
    query: 'work',
    includeSensitive: false,
  })

  assert.equal(calls.length, 1)
  assert.equal(
    calls[0].url,
    'http://mnemotrace.test/v1/memories?facts_limit=2&facts_offset=4&episodes_limit=0&status=live&q=work&include_sensitive=false&space_id=space-test',
  )
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.equal(dump.facts_page.next_offset, 6)
  assert.equal(dump.episodes.length, 0)
  assert.equal(dump.facts[0].sensitive, false)
})

test('export defaults to share-safe payload and can request full private export', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    const includeSensitive = url.endsWith('include_sensitive=true&space_id=space-test')
    return new Response(JSON.stringify({
      include_sensitive: includeSensitive,
      redacted_sensitive: !includeSensitive,
      facts: includeSensitive
        ? [{ object: 'Moonshot AI' }, { object: 'diabetes', sensitive: true }]
        : [{ object: 'Moonshot AI' }],
      graph: { nodes: [], edges: [] },
      episodes: includeSensitive ? [{ content: 'private raw text' }] : [],
      profile: includeSensitive ? 'private profile' : '',
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test/v1',
    apiKey: 'sk-test',
    fetch,
  })

  const safeExport = await client.export(space)
  const fullExport = await client.export({ ...space, includeSensitive: true })

  assert.equal(calls[0].url, 'http://mnemotrace.test/v1/export?include_sensitive=false&space_id=space-test')
  assert.equal(calls[1].url, 'http://mnemotrace.test/v1/export?include_sensitive=true&space_id=space-test')
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.equal('mnemotrace_export_version' in safeExport, false)
  assert.equal(Object.hasOwn(safeExport, 'engram_export_version'), false)
  assert.equal(safeExport.include_sensitive, false)
  assert.equal(safeExport.redacted_sensitive, true)
  assert.deepEqual(safeExport.facts.map((fact) => fact.object), ['Moonshot AI'])
  assert.equal(fullExport.include_sensitive, true)
  assert.deepEqual(fullExport.facts.map((fact) => fact.object), ['Moonshot AI', 'diabetes'])
})

test('forget requires explicit confirmation before sending destructive request', async () => {
  const calls = []
  const fetch = async (url, init = {}) => {
    calls.push({ url, init })
    return new Response(JSON.stringify({
      ok: true,
      message: 'All memory erased.',
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  const client = new MnemoTraceClient({
    baseUrl: 'http://mnemotrace.test/v1',
    apiKey: 'sk-test',
    fetch,
  })

  assert.throws(
    () => client.forget(),
    (err) => err.name === 'MnemoTraceError' && err.status === 400 && /confirm/.test(err.message),
  )
  assert.equal(calls.length, 0)

  const done = await client.forget({ ...space, confirm: true })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'http://mnemotrace.test/v1/forget')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers.Authorization, 'Bearer sk-test')
  assert.deepEqual(JSON.parse(calls[0].init.body), { confirm: true, space_id: 'space-test' })
  assert.equal(done.ok, true)
})


test('memory browse sends outcome and complete context and exposes the response context', async () => {
  const calls = []
  const client = new MnemoTraceClient({ fetch: async url => {
    calls.push(new URL(url))
    return new Response(JSON.stringify({ data_revision: 'synthetic-v1', evaluated_at: 1000, facts: [] }))
  } })
  const dump = await client.memories({ ...space, outcomeKind: 'lesson', expectedRevision: 'synthetic-v1', evaluatedAt: 1000, factsOffset: 60, factsLimit: 60 })
  assert.equal(calls[0].searchParams.get('outcome_kind'), 'lesson')
  assert.equal(calls[0].searchParams.get('expected_revision'), 'synthetic-v1')
  assert.equal(calls[0].searchParams.get('evaluated_at'), '1000')
  assert.equal(calls[0].searchParams.get('facts_offset'), '60')
  assert.equal(dump.data_revision, 'synthetic-v1')
  assert.equal(dump.evaluated_at, 1000)
})

test('memory revision conflict retains the structured HTTP error without automatic retry', async () => {
  let calls = 0
  const body = { detail: { code: 'memory_revision_changed', message: 'Synthetic revision changed' } }
  const client = new MnemoTraceClient({ fetch: async () => {
    calls++
    return new Response(JSON.stringify(body), { status: 409 })
  } })
  await assert.rejects(client.memories({ ...space, expectedRevision: 'synthetic-v1', evaluatedAt: 1000 }), error => {
    assert.ok(error instanceof MnemoTraceError)
    assert.equal(error.status, 409)
    assert.deepEqual(error.detail, body)
    return true
  })
  assert.equal(calls, 1)
})


test('original lookup encodes the event ID and preserves space selection', async () => {
  const calls = []
  const client = new MnemoTraceClient({fetch: async (url, init) => {
    calls.push({url, body: init.body && JSON.parse(init.body)})
    return new Response(JSON.stringify({id: 'event/id', content: 'complete original'}))
  }})
  assert.equal((await client.getEpisode('event/id', {spaceId: 'shared space'})).content, 'complete original')
  assert.equal(new URL(calls[0].url).pathname, '/v1/episodes/event%2Fid')
  assert.equal(new URL(calls[0].url).searchParams.get('space_id'), 'shared space')
  await client.flushProcessing({...space, includeDigests: true})
  assert.equal(calls[1].body.include_digests, true)
})

test('queries and retries the same scoped operation ID', async () => {
  const requests = []
  const client = new MnemoTraceClient({ fetch: async (url, init) => {
    requests.push({ url: new URL(url), body: init.body && JSON.parse(init.body) })
    return Response.json({ operation_id: 'synthetic-op', core_committed: false, stored_raw: true })
  } })
  assert.equal((await client.getOperation('synthetic-op', space)).stored_raw, true)
  assert.equal((await client.retryOperation('synthetic-op', space)).operation_id, 'synthetic-op')
  assert.equal(requests[0].url.searchParams.get('space_id'), space.spaceId)
  assert.equal(requests[1].url.pathname, '/v1/operations/synthetic-op/retry')
  assert.deepEqual(requests[1].body, { space_id: space.spaceId })
})


test('structured reads use explicit scoped tools and forward time conditions', async () => {
  const calls = []
  const client = new MnemoTraceClient({fetch: async (url, init) => {
    calls.push([new URL(url).pathname, JSON.parse(init.body)])
    return new Response('{}', {status: 200})
  }})
  await client.aggregateFacts('visited', {...space, operation: 'count_distinct', start: 1, limit: 2})
  await client.followPath('user', ['sister', 'works_at'], {...space, valid_at: 20, redact_sensitive: true})
  assert.deepEqual(calls, [
    ['/v1/aggregate', {space_id: 'space-test', predicate: 'visited', operation: 'count_distinct', start: 1, limit: 2}],
    ['/v1/follow-path', {space_id: 'space-test', anchor: 'user', predicates: ['sister', 'works_at'], valid_at: 20, redact_sensitive: true}],
  ])
})
