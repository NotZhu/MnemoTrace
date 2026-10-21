const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const rq = require('@tanstack/react-query')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/hooks/queries.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['@tanstack/react-query', '../lib/api', '../store/auth'],
}).outputFiles[0].text

const full = { facts: [{ id: 'synthetic-private', sensitive: true }], fact_categories: ['private'], facts_page: { total: 1, has_more: true } }
const safe = { facts: [{ id: 'synthetic-public', sensitive: false }], fact_categories: ['public'], facts_page: { total: 1, has_more: false } }
const initial = { kind: 'attributes', include_sensitive: true, status: 'live', facts_limit: 40, episodes_limit: 0 }

function setup(params = initial, data = full) {
  let namespace = 'synthetic-A'
  const pending = []
  const useAuth = selector => selector({ apiKey: namespace })
  useAuth.getState = () => ({ apiKey: namespace })
  const moduleData = { exports: {} }
  vm.runInNewContext(source, { module: moduleData, exports: moduleData.exports, require(name) {
    if (name === '@tanstack/react-query') return { ...rq, useQuery: options => options }
    if (name === '../store/auth') return { useAuth }
    if (name === '../lib/api') return { api: { memories: params => new Promise((resolve, reject) => pending.push({ params, resolve, reject })) } }
    throw new Error(name)
  } })
  const h = moduleData.exports
  const client = new rq.QueryClient({ defaultOptions: { queries: { staleTime: Infinity, gcTime: 0, retry: false } } })
  client.setQueryData(h.qk.memories(params), data)
  const observer = new rq.QueryObserver(client, h.useMemories(params, { keepPreviousData: true }))
  const unsubscribe = observer.subscribe(() => {})
  return {
    pending, observer, client, h,
    change(params, account) { if (account) namespace = account; observer.setOptions(h.useMemories(params, { keepPreviousData: true })); return observer.getCurrentResult() },
    close() { unsubscribe(); observer.destroy(); client.clear() },
  }
}

for (const [name, patch] of Object.entries({
  sensitive: { include_sensitive: false }, kind: { kind: 'outcomes' }, category: { category: 'work' },
  status: { status: 'superseded' }, factsOffset: { facts_offset: 40 }, episodesOffset: { episodes_offset: 20 },
  futureFilter: { future_filter: 'restricted' },
})) {
  test(`scope: ${name} cannot borrow an incompatible response`, () => {
    const s = setup()
    try {
      const result = s.change({ ...initial, ...patch })
      assert.equal(result.data, undefined)
      assert.equal(result.isLoading, true)
      assert.equal(result.isPlaceholderData, false)
    } finally { s.close() }
  })
}

test('scope: widening also waits; omitted sensitive equals false and empty category equals omitted', () => {
  const s = setup({ ...initial, include_sensitive: false }, safe)
  try {
    assert.equal(s.change({ ...initial, include_sensitive: undefined, category: '', q: 'new' }).data, safe)
    assert.equal(s.change(initial).data, undefined)
  } finally { s.close() }
})

test('scope: search and page size retain data but another account never does', () => {
  const s = setup()
  try {
    const r = s.change({ ...initial, q: 'new', facts_limit: 80, episodes_limit: 10 })
    assert.equal(r.data, full)
    assert.equal(r.isPlaceholderData, true)
    assert.equal(s.change(initial, 'synthetic-B').data, undefined)
  } finally { s.close() }
})

test('scope: Journal search retains results; changing history scope waits', () => {
  const params = { ...initial, kind: 'outcomes' }, s = setup(params)
  try {
    assert.equal(s.change({ ...params, q: 'lesson' }).isPlaceholderData, true)
    assert.equal(s.change({ ...params, status: undefined, q: 'lesson' }).data, undefined)
  } finally { s.close() }
})

const flush = () => new Promise(resolve => setImmediate(resolve))

test('scope: delayed failure and explicit retry never restore sensitive placeholder', async () => {
  const s = setup()
  try {
    assert.equal(s.change({ ...initial, include_sensitive: false }).data, undefined)
    assert.equal(s.pending[0].params.include_sensitive, false)
    s.pending[0].reject(new Error('synthetic failure'))
    await flush()
    assert.equal(s.observer.getCurrentResult().isError, true)
    assert.equal(s.observer.getCurrentResult().data, undefined)
    const retry = s.observer.refetch()
    assert.equal(s.observer.getCurrentResult().data, undefined)
    s.pending[1].resolve(safe)
    await retry
    assert.deepEqual(s.observer.getCurrentResult().data, safe)
  } finally { s.close() }
})

test('scope: rapid toggles and late wide response cannot replace current safe data', async () => {
  const s = setup(), hidden = { ...initial, include_sensitive: false }
  try {
    s.change({ ...initial, q: 'wide' })
    s.change(hidden)
    assert.equal(s.observer.getCurrentResult().data, undefined)
    assert.equal(s.change(initial).data, full) // Exact cache remains valid when explicitly showing all.
    s.change(hidden)
    s.pending.find(p => p.params.include_sensitive === false).resolve(safe)
    await flush()
    s.pending.find(p => p.params.q === 'wide').resolve(full)
    await flush()
    assert.deepEqual(s.observer.getCurrentResult().data, safe)
    assert.equal(s.observer.getCurrentResult().isPlaceholderData, false)
  } finally { s.close() }
})

test('scope: exact safe cache is immediately usable', () => {
  const s = setup(), hidden = { ...initial, include_sensitive: false }
  try {
    s.client.setQueryData(s.h.qk.memories(hidden), safe)
    const r = s.change(hidden)
    assert.deepEqual(r.data, safe)
    assert.equal(r.isPlaceholderData, false)
  } finally { s.close() }
})
