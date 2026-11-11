const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

// 实际 API 与登录 store 共用一个 bundle；只替换网络和浏览器下载边界。
const source = esbuild.buildSync({
  stdin: {
    contents: "export { api, ApiError, StaleSessionError } from './lib/api'; export * from './store/auth'",
    resolveDir: path.resolve(__dirname, '../src'), loader: 'ts',
  },
  bundle: true, platform: 'node', format: 'cjs', write: false, external: ['react'],
  define: { 'import.meta.env.VITE_API_BASE': '""' },
}).outputFiles[0].text

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup(options = {}) {
  let handler
  const calls = { requests: [], urls: [], clicks: [], removed: 0, revoked: [] }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports, require, Headers, URLSearchParams, AbortController, AbortSignal,
    fetch: (url, init) => { calls.requests.push({ url, init }); return handler(url, init) },
    URL: {
      createObjectURL(blob) { calls.urls.push(blob); options.onCreate?.(); return 'blob:synthetic' },
      revokeObjectURL(url) { calls.revoked.push(url) },
    },
    document: {
      body: { appendChild() { options.onAppend?.() } },
      createElement() {
        options.onElement?.()
        return {
          click() { options.onClick?.(); calls.clicks.push(this.download) },
          remove() { calls.removed++ },
        }
      },
    },
  })
  return {
    ...moduleData.exports, calls,
    respond: next => { handler = next },
    login: key => {
      const auth = moduleData.exports.useAuth.getState()
      auth.login(key)
      if (moduleData.exports.useAuth.getState().apiKey) auth.selectSpace('synthetic-space')
    },
    logout: () => moduleData.exports.useAuth.getState().logout(),
  }
}

test('notes and dialogue send a boolean extract flag', async () => {
  const s = setup()
  s.login('synthetic-A')
  s.respond(async () => new Response(JSON.stringify({ ok: true })))
  const messages = [{ message_id: 'm1', role: 'user', content: 'Synthetic dialogue' }]
  await s.api.remember('Synthetic note')
  await s.api.remember('Synthetic raw note', { extract: false })
  await s.api.rememberMessages(messages, 's', false)
  assert.deepEqual(s.calls.requests.map(({ init }) => JSON.parse(init.body).extract), [true, false, false])
})

const transitions = {
  logout: s => s.logout(),
  'A to B': s => s.login('synthetic-B'),
  'A logout A': s => { s.logout(); s.login('synthetic-A') },
  'A B A': s => { s.login('synthetic-B'); s.login('synthetic-A') },
  'same key login': s => s.login('synthetic-A'),
}

for (const phase of ['response', 'body']) {
  for (const [name, transition] of Object.entries(transitions)) {
    test(`export during ${phase}: ${name} discards the old download`, async () => {
      const s = setup(), paused = deferred(), entered = deferred()
      s.login('synthetic-A')
      const response = { ok: true, status: 200, blob: async () => {
        if (phase === 'body') { entered.resolve(); await paused.promise }
        return new Blob(['synthetic private memory'])
      } }
      s.respond(async () => {
        if (phase === 'response') { entered.resolve(); await paused.promise }
        return response
      })
      const pending = s.api.exportDownload('synthetic', true)
      await entered.promise
      transition(s)
      paused.resolve()
      await assert.rejects(pending, error => error instanceof s.StaleSessionError && error instanceof s.ApiError && error.status === 409)
      assert.equal(s.calls.urls.length, 0)
      assert.equal(s.calls.clicks.length, 0)
    })
  }
}

test('login and logout atomically publish a new in-memory session, including invalid and same-key login', () => {
  const s = setup(), observed = []
  assert.equal(s.currentSession().epoch, 0)
  s.useAuth.subscribe(state => observed.push({ key: state.apiKey, epoch: state.epoch }))
  s.useAuth.getState().login('synthetic-A')
  const first = s.currentSession()
  s.useAuth.getState().login('synthetic-A')
  assert.equal(s.isCurrentSession(first), false)
  s.useAuth.getState().login('invalid\nkey')
  assert.ok(s.useAuth.getState().keyError)
  s.logout()
  s.logout()
  assert.deepEqual(observed, [
    { key: 'synthetic-A', epoch: 1 }, { key: 'synthetic-A', epoch: 2 },
    { key: null, epoch: 3 }, { key: null, epoch: 4 }, { key: null, epoch: 5 },
  ])
  assert.equal(first.key, 'synthetic-A')
  assert.equal(first.epoch, 1)
})

for (const method of ['memories', 'exportDownload']) {
  for (const outcome of [200, 401, 500, 'network-error']) {
    test(`${method}: old ${outcome} response after same-key relogin is stale and cannot log out`, async () => {
      const s = setup(), pendingResponse = deferred()
      s.login('synthetic-A')
      s.respond(() => pendingResponse.promise)
      const pending = s.api[method]()
      s.logout()
      s.login('synthetic-A')
      const current = s.currentSession()
      if (outcome === 'network-error') pendingResponse.reject(new TypeError('synthetic network failure'))
      else pendingResponse.resolve({ ok: outcome === 200, status: outcome,
        json: async () => ({ detail: 'synthetic old error' }), blob: async () => new Blob(['synthetic']) })
      await assert.rejects(pending, error => error instanceof s.StaleSessionError && error.status === 409)
      assert.equal(s.isCurrentSession(current), true)
      assert.equal(s.calls.clicks.length, 0)
    })
  }
}

for (const outcome of ['json', 'json-error', 'error-detail', 'error-detail-rejection']) {
  test(`JSON body ${outcome} cannot cross a same-key login`, async () => {
    const s = setup(), body = deferred(), entered = deferred()
    s.login('synthetic-A')
    const status = outcome.startsWith('error-detail') ? 500 : 200
    s.respond(async () => ({ ok: status === 200, status, json: () => { entered.resolve(); return body.promise } }))
    const pending = s.api.memories()
    await entered.promise
    s.login('synthetic-A')
    if (outcome === 'json-error' || outcome === 'error-detail-rejection') body.reject(new Error('synthetic body failure'))
    else body.resolve({ detail: 'synthetic old content' })
    await assert.rejects(pending, error => error instanceof s.StaleSessionError)
  })
}

for (const stage of ['response', 'body']) {
  test(`public health ${stage} remains valid across login, logout and relogin`, async () => {
    const s = setup(), paused = deferred(), entered = deferred()
    const response = { ok: true, status: 200, json: async () => {
      if (stage === 'body') { entered.resolve(); await paused.promise }
      return { ok: true }
    } }
    s.respond(async () => {
      if (stage === 'response') { entered.resolve(); await paused.promise }
      return response
    })
    const pending = s.api.health()
    await entered.promise
    s.login('synthetic-A'); s.logout(); s.login('synthetic-A'); s.login('synthetic-B')
    paused.resolve()
    assert.equal((await pending).ok, true)
    assert.equal(s.currentKey(), 'synthetic-B')
  })
}

test('old public health 401 cannot log out a newer same-key login', async () => {
  const s = setup(), response = deferred()
  s.login('synthetic-A')
  s.respond(() => response.promise)
  const pending = s.api.health()
  s.login('synthetic-A')
  response.resolve({ ok: false, status: 401 })
  await assert.rejects(pending, error => error.status === 401)
  assert.equal(s.currentKey(), 'synthetic-A')
})

test('current JSON 401 logs out once and keeps the unauthorized error contract', async () => {
  const s = setup()
  s.login('synthetic-A')
  const epoch = s.currentSession().epoch
  s.respond(async () => ({ ok: false, status: 401 }))
  await assert.rejects(s.api.memories(), error => error.status === 401 && !(error instanceof s.StaleSessionError))
  assert.equal(s.currentKey(), null)
  assert.equal(s.currentSession().epoch, epoch + 1)
})

test('current JSON errors retain server details, invalid JSON fallback and network errors', async () => {
  const s = setup()
  s.login('synthetic-A')
  s.respond(async () => ({ ok: false, status: 422, json: async () => ({ detail: 'synthetic detail' }) }))
  await assert.rejects(s.api.memories(), error => error.status === 422 && error.message === 'synthetic detail')
  s.respond(async () => ({ ok: false, status: 500, json: async () => { throw new Error('not JSON') } }))
  await assert.rejects(s.api.memories(), error => error.status === 500)
  const failure = new TypeError('synthetic offline')
  s.respond(async () => { throw failure })
  await assert.rejects(s.api.memories(), error => error === failure)
  s.respond(async () => ({ ok: true, status: 204 }))
  assert.equal(await s.api.memories(), undefined)
})

for (const sensitive of [false, true]) {
  test(`normal export sensitive=${sensitive} sends the original key and releases its URL`, async () => {
    const s = setup(), blob = new Blob(['synthetic memory'])
    s.login('synthetic-A')
    s.respond(async () => ({ ok: true, status: 200, blob: async () => blob }))
    await s.api.exportDownload('synthetic', sensitive)
    const [{ url, init }] = s.calls.requests
    assert.equal(new URL(url, 'http://test').searchParams.get('include_sensitive'), String(sensitive))
    assert.equal(new Headers(init.headers).get('Authorization'), 'Bearer synthetic-A')
    assert.equal(s.calls.urls[0], blob)
    assert.deepEqual(s.calls.clicks, ['mnemotrace_synthetic_export.json'])
    assert.equal(s.calls.removed, 1)
    assert.deepEqual(s.calls.revoked, ['blob:synthetic'])
  })
}

for (const stage of ['onElement', 'onAppend', 'onClick']) {
  test(`download failure at ${stage} still releases the URL and removes any element`, async () => {
    const failure = new Error('synthetic DOM failure')
    const s = setup({ [stage]: () => { throw failure } })
    s.login('synthetic-A')
    s.respond(async () => ({ ok: true, status: 200, blob: async () => new Blob(['synthetic']) }))
    await assert.rejects(s.api.exportDownload(), error => error === failure)
    assert.equal(s.calls.clicks.length, 0)
    assert.equal(s.calls.removed, stage === 'onElement' ? 0 : 1)
    assert.deepEqual(s.calls.revoked, ['blob:synthetic'])
  })
}

test('a session change after URL creation cancels the click and releases resources', async () => {
  const s = setup({ onCreate: () => s.login('synthetic-A') })
  s.login('synthetic-A')
  s.respond(async () => ({ ok: true, status: 200, blob: async () => new Blob(['synthetic']) }))
  await assert.rejects(s.api.exportDownload(), error => error instanceof s.StaleSessionError)
  assert.equal(s.calls.clicks.length, 0)
  assert.equal(s.calls.removed, 1)
  assert.deepEqual(s.calls.revoked, ['blob:synthetic'])
})

for (const stale of [false, true]) {
  test(`Blob read failure stale=${stale} has no URL to release`, async () => {
    const s = setup(), body = deferred(), entered = deferred(), failure = new Error('synthetic Blob failure')
    s.login('synthetic-A')
    s.respond(async () => ({ ok: true, status: 200, blob: () => { entered.resolve(); return body.promise } }))
    const pending = s.api.exportDownload()
    await entered.promise
    if (stale) s.login('synthetic-A')
    body.reject(failure)
    await assert.rejects(pending, error => stale ? error instanceof s.StaleSessionError : error === failure)
    assert.equal(s.calls.urls.length, 0)
    assert.deepEqual(s.calls.revoked, [])
  })
}


test('memory browse forwards cancellation and the complete browsing context to fetch', async () => {
  const s = setup(), controller = new AbortController()
  s.login('synthetic-A')
  s.respond(async () => ({ ok: true, status: 200, json: async () => ({ data_revision: 'synthetic-v1', evaluated_at: 1000 }) }))
  const result = await s.api.memories({ outcome_kind: 'lesson', expected_revision: 'synthetic-v1', evaluated_at: 1000 }, { signal: controller.signal })
  assert.equal(s.calls.requests[0].init.signal.aborted, false)
  controller.abort()
  assert.equal(s.calls.requests[0].init.signal.aborted, true)
  const url = s.calls.requests[0].url
  assert.ok(url.includes('outcome_kind=lesson') && url.includes('expected_revision=synthetic-v1') && url.includes('evaluated_at=1000'))
  assert.equal(result.evaluated_at, 1000)
})

test('switching space with the same key aborts the old scoped request', async () => {
  const s = setup()
  s.login('synthetic-A')
  const response = deferred()
  s.respond(() => response.promise)
  const pending = s.api.memories()
  assert.match(s.calls.requests[0].url, /space_id=synthetic-space/)
  s.useAuth.getState().selectSpace('synthetic-other')
  assert.equal(s.calls.requests[0].init.signal.aborted, true)
  response.resolve({ok: true, json: async () => ({facts: []})})
  await assert.rejects(pending, s.StaleSessionError)
})
