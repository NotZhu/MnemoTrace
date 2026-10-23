const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const rq = require('@tanstack/react-query')

const source = esbuild.buildSync({
  stdin: { contents: "export * from './hooks/memoryPages'; export * from './hooks/queries'", resolveDir: path.resolve(__dirname, '../src'), loader: 'ts' },
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['@tanstack/react-query', '../lib/api', '../store/auth'],
}).outputFiles[0].text
class RevisionError extends Error {}
class SessionError extends Error {}
const flush = () => new Promise(resolve => setImmediate(resolve))
function setup({ episodes = false, count = 601, holdInitial = false } = {}) {
  let revision = 'synthetic-v1', clock = 1000, failureOffset, malformed = false, epoch = 1, namespace = 'synthetic-A'
  let pending, observer, releaseInitial, commits = 1
  if (holdInitial) pending = new Promise((resolve, reject) => { releaseInitial = error => error ? reject(error) : resolve() })
  const calls = [], errors = [], rows = Array.from({ length: count }, (_, id) => ({ id: `synthetic-${id}`, predicate: id < 61 ? 'decision' : 'lesson' }))
  const client = new rq.QueryClient({ queryCache: new rq.QueryCache({ onError: error => errors.push(error) }), defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 10_000 } } })
  const api = { async memories(params, options = {}) {
    const collection = episodes ? 'episodes' : 'facts', offset = params[`${collection}_offset`] || 0
    const version = revision, evaluatedAt = params.evaluated_at ?? clock, limit = params[`${collection}_limit`]
    calls.push({ ...params, signal: options.signal })
    const filtered = rows.filter(row => !params.outcome_kind || row.predicate === params.outcome_kind)
    const items = filtered.slice(offset, offset + limit), next = offset + items.length
    const page = { items, total: filtered.length, offset, limit, has_more: next < filtered.length, next_offset: next < filtered.length ? next : null }
    if (malformed) page.next_offset = offset
    const result = { user: 'synthetic-user', data_revision: version, evaluated_at: evaluatedAt,
      facts: [], episodes: [], [collection]: items, [`${collection}_page`]: page }
    // Intentionally ignore abort: cancellation must also prevent committing a late response.
    if (pending) { const wait = pending; pending = undefined; await wait }
    if (failureOffset === offset) { failureOffset = undefined; throw new Error('synthetic network failure') }
    if (params.expected_revision && params.expected_revision !== version) throw new RevisionError('记录已变化，请重新加载')
    return result
  } }
  const mod = { exports: {} }
  vm.runInNewContext(source, { module: mod, exports: mod.exports, require(name) {
    if (name === '@tanstack/react-query') return { ...rq,
      useQueryClient: () => client,
      useQuery: options => options,
      useMutation(options) {
        const mutation = new rq.MutationObserver(client, { ...options, gcTime: 0 })
        return { mutateAsync: variables => mutation.mutate(variables) }
      },
      // Adapt React subscription only; the query lifecycle uses the real library.
      useInfiniteQuery(options) { observer.setOptions(options); return observer.getCurrentResult() },
    }
    if (name === '../lib/api') return { api, MemoryRevisionChangedError: RevisionError, StaleSessionError: SessionError }
    if (name === '../store/auth') {
      const useAuth = select => select({ apiKey: namespace })
      useAuth.getState = () => ({ apiKey: namespace })
      return { useAuth,
      currentSession: () => ({ key: namespace, epoch }),
      isCurrentSession: session => session.epoch === epoch && session.key === namespace,
    }
    }
    throw new Error(name)
  } })
  const commit = async () => {
    revision = `synthetic-v${++commits}`
    const id = commits === 2 ? 'synthetic-written' : `synthetic-written-${commits}`
    rows.unshift({ id, predicate: 'decision' })
    return { id }
  }
  Object.assign(api, { addFact: commit, editFact: commit, deleteFact: commit, clearSlot: commit,
    remember: commit, closeSession: commit, forget: commit, setFocus: commit, resolveConflict: commit, setPolicy: commit })
  const h = mod.exports
  let params = episodes ? { facts_limit: 0, episodes_limit: 20 } : { facts_limit: 60, episodes_limit: 0, kind: 'outcomes' }
  observer = new rq.InfiniteQueryObserver(client, h.memoryPagesOptions(namespace, params))
  const off = observer.subscribe(() => {})
  const actions = () => h.useMemoryPages(params)
  return { h, client, observer, calls, errors, rows, api, actions, releaseInitial,
    write: (entry = 'useAddFact') => h[entry]().mutateAsync(entry === 'useRemember' ? 'Synthetic write' :
      { id: 'synthetic-existing', patch: { object: 'Synthetic edit' }, subject: 'synthetic', predicate: 'marker', object: 'Synthetic write' }),
    async ready() { await observer.refetch() },
    current: () => observer.getCurrentResult(),
    change(patch, key = namespace) { params = { ...params, ...patch }; namespace = key; observer.setOptions(h.memoryPagesOptions(namespace, params)); return observer.getCurrentResult() },
    revision(value) { revision = value }, tick() { clock++ }, relogin() { epoch++ },
    fail(offset = 0) { failureOffset = offset }, malformed() { malformed = true },
    hold() { let resume; pending = new Promise((resolve, reject) => { resume = error => error ? reject(error) : resolve() }); return resume },
    restart: () => actions().reload(),
    close() { off(); observer.destroy(); client.clear() },
  }
}

for (const episodes of [false, true]) test(`complete ${episodes ? 'episodes' : 'facts'} use fixed pages, exact IDs and browse context`, async () => {
  const s = setup({ episodes })
  try {
    await s.ready()
    while (s.current().hasNextPage) await s.actions().loadMore()
    const data = s.h.mergeMemoryPages(s.current().data), collection = episodes ? 'episodes' : 'facts'
    assert.deepEqual(Array.from(data[collection], row => row.id), s.rows.map(row => row.id))
    assert.equal(new Set(data[collection].map(row => row.id)).size, 601)
    assert.ok(s.calls.every(call => call[`${collection}_limit`] === (episodes ? 20 : 60)))
    assert.ok(s.calls.filter(call => call[`${collection}_offset`] > 0).every(call => call.expected_revision === 'synthetic-v1' && call.evaluated_at === 1000))
  } finally { s.close() }
})

test('a failed next page retains rows; retry and duplicate clicks request the same position once', async () => {
  const s = setup()
  try {
    await s.ready(); const before = s.current().data
    s.fail(60); await s.actions().loadMore()
    assert.equal(s.current().data, before)
    assert.equal(s.current().isFetchNextPageError, true)
    const release = s.hold(), one = s.actions().refetch(), two = s.actions().refetch()
    release(); await Promise.all([one, two])
    assert.equal(s.current().data.pages.length, 2)
    assert.deepEqual(s.calls.map(call => call.facts_offset), [0, 60, 60])
  } finally { s.close() }
})

test('type is filtered before pagination and changing scope starts at zero without borrowing rows', async () => {
  const s = setup({ count: 62 })
  try {
    await s.ready()
    assert.equal(s.change({ outcome_kind: 'lesson' }).data, undefined)
    await s.observer.refetch()
    const data = s.h.mergeMemoryPages(s.current().data)
    assert.equal(data.facts.length, 1); assert.equal(data.facts_page.total, 1)
    assert.equal(s.calls.at(-1).facts_offset, 0)
    assert.equal(s.change({ include_sensitive: true }).data, undefined)
    assert.equal(s.change({}, 'synthetic-B').data, undefined)
  } finally { s.close() }
})

test('late filter response cannot replace a new scope; search placeholder is explicitly marked', async () => {
  const s = setup()
  try {
    await s.ready(); const release = s.hold()
    assert.equal(s.change({ q: 'old' }).isPlaceholderData, true)
    assert.equal(s.change({ outcome_kind: 'lesson', q: 'new' }).data, undefined)
    await s.observer.refetch(); const selected = s.current().data
    release(); await flush()
    assert.equal(s.current().data, selected)
    assert.equal(selected.pages[0].facts[0].predicate, 'lesson')
  } finally { s.close() }
})

test('reload within the production staleTime makes requests and keeps all loaded pages', async () => {
  const s = setup()
  try {
    await s.ready(); await s.actions().loadMore(); await s.actions().loadMore()
    const before = s.calls.length
    s.revision('synthetic-v2'); s.tick(); await s.restart()
    assert.equal(s.calls.length - before, 3)
    assert.equal(s.current().data.pages.length, 3)
    assert.ok(s.current().data.pages.every(page => page.data_revision === 'synthetic-v2' && page.evaluated_at === 1001))
    assert.equal(s.calls[before].expected_revision, undefined)
    assert.equal(s.calls[before].evaluated_at, undefined)
    assert.equal(s.client.getQueryCache().getAll()[0].options.pages, undefined)
  } finally { s.close() }
})

for (const trigger of ['reload', 'invalidation']) for (const lateFails of [false, true]) {
  test(`${trigger} supersedes an older reload even when transport ignores abort and late response ${lateFails ? 'fails' : 'succeeds'}`, async () => {
    const s = setup()
    try {
      await s.ready(); await s.actions().loadMore()
      s.revision('synthetic-v2'); const release = s.hold(), older = s.restart(); await flush()
      s.revision('synthetic-v3')
      if (trigger === 'reload') await s.restart()
      else await s.client.invalidateQueries({ queryKey: ['memories', 'synthetic-A'] })
      const current = s.current().data
      release(lateFails ? new Error('late failure') : undefined); await older; await flush()
      assert.equal(s.current().data, current)
      assert.ok(current.pages.every(page => page.data_revision === 'synthetic-v3'))
      assert.equal(current.pages.length, 2)
      assert.equal(s.current().error, null)
      assert.equal(s.calls[2].signal?.aborted, true)
    } finally { s.close() }
  })
}

test('a later-page reload failure keeps the complete old object and cannot poison future invalidation', async () => {
  const s = setup()
  try {
    await s.ready(); await s.actions().loadMore(); await s.actions().loadMore()
    const before = s.current().data
    s.revision('synthetic-v2'); s.fail(60); await s.restart()
    assert.equal(s.current().data, before); assert.equal(s.current().isError, true)
    assert.equal(s.client.getQueryCache().getAll()[0].options.pages, undefined)
    await s.client.invalidateQueries({ queryKey: ['memories', 'synthetic-A'] })
    assert.equal(s.current().data.pages.length, 3)
    assert.ok(s.current().data.pages.every(page => page.data_revision === 'synthetic-v2'))
    assert.equal(s.current().error, null)
  } finally { s.close() }
})

test('a shorter refreshed collection stops at its new last page', async () => {
  const s = setup()
  try {
    await s.ready(); await s.actions().loadMore(); await s.actions().loadMore()
    s.rows.splice(61); s.revision('synthetic-v2'); await s.restart()
    assert.equal(s.current().data.pages.length, 2)
    assert.equal(s.h.mergeMemoryPages(s.current().data).facts.length, 61)
    assert.equal(s.current().hasNextPage, false)
  } finally { s.close() }
})

test('revision conflict retains old rows; failed refresh retains them and successful refresh rebuilds the chain', async () => {
  const s = setup()
  try {
    await s.ready(); await s.actions().loadMore(); const old = s.current().data
    s.revision('synthetic-v2'); await s.actions().loadMore()
    assert.ok(s.current().error instanceof RevisionError); assert.equal(s.current().data, old)
    assert.equal(s.actions().canLoadMore, false)
    s.fail(); await s.restart(); assert.equal(s.current().data, old)
    const release = s.hold(), reload = s.restart(); await flush()
    assert.equal(s.current().data, old); release(); await reload
    assert.equal(s.current().data.pages.length, 2)
    assert.equal(s.current().error, null)
  } finally { s.close() }
})

test('malformed offset or mismatched evaluation time cannot append a page', async () => {
  const s = setup()
  try {
    await s.ready(); const old = s.current().data
    const original = s.api.memories
    s.api.memories = async (...args) => ({ ...await original(...args), evaluated_at: 1234 })
    await s.actions().loadMore()
    assert.ok(s.current().error instanceof s.h.PaginationProtocolError)
    assert.equal(s.current().data, old)
    s.api.memories = original; await s.restart(); s.malformed()
    await s.actions().loadMore()
    assert.ok(s.current().error instanceof s.h.PaginationProtocolError)
  } finally { s.close() }
})

test('reload finishing after a new login cycle cannot recreate a removed private cache', async () => {
  const s = setup()
  try {
    await s.ready(); const release = s.hold(), reload = s.restart(); await flush()
    s.relogin(); await s.client.cancelQueries({ queryKey: ['memories', 'synthetic-A'] })
    s.client.removeQueries({ queryKey: ['memories', 'synthetic-A'] })
    release(); await reload
    assert.equal(s.client.getQueryCache().getAll().length, 0)
  } finally { s.close() }
})


test('write during an uncached filter first page must fetch the new commit', async () => {
  const s = setup()
  let release
  try {
    await s.ready()
    release = s.hold()
    const start = s.calls.length
    s.change({ category: 'synthetic-new-scope' })
    await flush()
    assert.equal(s.current().data, undefined)
    await s.write()
    release(); await flush()
    assert.equal(s.calls.length - start, 2)
    assert.equal(s.current().data.pages[0].data_revision, 'synthetic-v2')
    assert.equal(s.current().data.pages[0].facts[0].id, 'synthetic-written')
    assert.equal(s.current().data.pages[0].facts_page.total, 602)
  } finally { release?.(); s.close() }
})


for (const lateFails of [false, true]) test(`write during initial load rejects the old ${lateFails ? 'failure' : 'success'} even without cached data`, async () => {
  const s = setup({ holdInitial: true })
  try {
    assert.equal(s.current().data, undefined)
    await s.write(); await flush()
    assert.equal(s.calls.length, 2)
    assert.equal(s.current().data.pages[0].data_revision, 'synthetic-v2')
    s.releaseInitial(lateFails ? new Error('late stale error') : undefined); await flush()
    assert.equal(s.current().data.pages[0].facts[0].id, 'synthetic-written')
    assert.equal(s.current().data.pages[0].facts_page.total, 602)
    assert.equal(s.current().error, null)
    assert.equal(s.errors.length, 0)
    assert.equal(s.calls[0].signal.aborted, true)
  } finally { s.releaseInitial(); s.close() }
})

test('a failed replacement first page never falls back to the cancelled old success', async () => {
  const s = setup({ holdInitial: true })
  try {
    s.fail(0); await s.write(); await flush()
    assert.equal(s.current().data, undefined)
    assert.match(s.current().error.message, /network/)
    s.releaseInitial(); await flush()
    assert.equal(s.current().data, undefined)
    assert.equal(s.errors.length, 1)
    await s.actions().refetch()
    assert.equal(s.current().data.pages[0].data_revision, 'synthetic-v2')
    assert.equal(s.current().data.pages[0].facts_page.total, 602)
  } finally { s.releaseInitial(); s.close() }
})

test('consecutive writes cancel pending replacement first pages and only commit the newest version', async () => {
  const s = setup({ holdInitial: true })
  let releaseReplacement
  try {
    releaseReplacement = s.hold()
    await s.write(); await flush()
    assert.equal(s.current().data, undefined)
    await s.write(); await flush()
    assert.equal(s.current().data.pages[0].data_revision, 'synthetic-v3')
    releaseReplacement(); s.releaseInitial(); await flush()
    assert.equal(s.current().data.pages[0].facts_page.total, 603)
    assert.equal(s.current().data.pages[0].facts[0].id, 'synthetic-written-3')
    assert.equal(s.calls.length, 3)
    assert.equal(s.errors.length, 0)
  } finally { releaseReplacement?.(); s.releaseInitial(); s.close() }
})

test('write refresh keeps loaded depth and does not invalidate another namespace', async () => {
  const s = setup()
  try {
    await s.ready(); await s.actions().loadMore(); await s.actions().loadMore()
    s.client.setQueryData(['memories', 'synthetic-B'], { private: 'other space' })
    const start = s.calls.length
    await s.write(); await flush()
    assert.deepEqual(s.calls.slice(start).map(call => call.facts_offset), [0, 60, 120])
    assert.equal(s.current().data.pages.length, 3)
    assert.ok(s.current().data.pages.every(page => page.data_revision === 'synthetic-v2'))
    assert.equal(s.client.getQueryState(['memories', 'synthetic-B']).isInvalidated, false)
  } finally { s.close() }
})

test('single overview first loads use the same write cancellation barrier', async () => {
  const s = setup()
  let off, overview, release
  try {
    await s.ready(); release = s.hold()
    overview = new rq.QueryObserver(s.client, s.h.useMemories({ facts_limit: 1, episodes_limit: 0 }))
    off = overview.subscribe(() => {})
    assert.equal(overview.getCurrentResult().data, undefined)
    await s.write(); await flush()
    assert.equal(overview.getCurrentResult().data.data_revision, 'synthetic-v2')
    release(); await flush()
    assert.equal(overview.getCurrentResult().data.facts[0].id, 'synthetic-written')
  } finally { release?.(); off?.(); overview?.destroy(); s.close() }
})

test('a login cycle change during the cancellation barrier leaves the new cache untouched', async () => {
  const s = setup()
  let release
  try {
    await s.ready()
    const cancel = s.client.cancelQueries.bind(s.client), barrier = new Promise(resolve => { release = resolve })
    s.client.cancelQueries = async (...args) => { await cancel(...args); await barrier }
    await s.write(); s.relogin()
    const key = ['memories', 'synthetic-A', 'new-login']
    const fresh = { private: 'new login' }
    s.client.setQueryData(key, fresh)
    const count = s.calls.length
    release(); await flush()
    assert.equal(s.calls.length, count)
    assert.equal(s.client.getQueryData(key), fresh)
    assert.equal(s.client.getQueryState(key).isInvalidated, false)
  } finally { release?.(); s.close() }
})

for (const entry of ['useRemember', 'useCloseSession', 'useEditFact', 'useDeleteFact', 'useClearSlot',
  'useForget', 'useSetFocus', 'useResolveConflict', 'useSetPolicy']) {
  test(`${entry} refreshes an uncached first page through the shared barrier`, async () => {
    const s = setup({ holdInitial: true })
    try {
      await s.write(entry); await flush()
      assert.equal(s.current().data.pages[0].data_revision, 'synthetic-v2')
      assert.equal(s.calls.length, 2)
      s.releaseInitial(); await flush()
      assert.equal(s.current().data.pages[0].facts[0].id, 'synthetic-written')
    } finally { s.releaseInitial(); s.close() }
  })
}
