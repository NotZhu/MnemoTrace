const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')
const reactQuery = require('@tanstack/react-query')

// 走真实入口、鉴权 store、API 客户端与 QueryClient，仅替换浏览器挂载和网络。
const source = esbuild.buildSync({
  stdin: {
    contents: fs.readFileSync(path.resolve(__dirname, '../src/main.tsx'), 'utf8') + `
      export { queryClient }
      export { useAuth } from './store/auth'
      export { useQueryErrors } from './store/queryErrors'
      export { api } from './lib/api'
      export { StaleSessionError } from './lib/api'
      export { useSourceEpisodes } from './hooks/queries'
      export { GlobalErrorBanner } from './components/GlobalErrorBanner'
    `,
    resolveDir: path.resolve(__dirname, '../src'), loader: 'tsx',
  },
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'react-dom/client', 'react-router-dom', '@tanstack/react-query', './App', './ui'],
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.VITE_API_BASE': '""', 'import.meta.env.BASE_URL': '"/ui/"' },
}).outputFiles[0].text

const spaceA = 'synthetic-space-a', spaceB = 'synthetic-space-b'
const keyFor = space => ['memories', space, {}]
const response = data => ({ ok: true, status: 200, json: async () => data })
const turn = () => new Promise(resolve => setImmediate(resolve))

function setup(savedErrors = null) {
  const storage = () => {
    const data = new Map()
    return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key), values: () => [...data.values()] }
  }
  const localStorage = storage(), sessionStorage = storage()
  if (savedErrors !== null) sessionStorage.setItem('mnemotrace.query-errors', savedErrors)
  const requests = []
  let runtime, handler = async () => { throw new TypeError('synthetic network failure') }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports, Headers, URLSearchParams,
    setTimeout, clearTimeout, console, localStorage,
    window: { localStorage, sessionStorage }, document: { getElementById: () => ({}) },
    require(name) {
      if (name === 'react') return { ...React, useSyncExternalStore: (_subscribe, snapshot) => snapshot(), useCallback: callback => callback, useDebugValue() {} }
      if (name === '@tanstack/react-query') return { ...reactQuery, useQueryClient: () => runtime.queryClient, useQuery: options => options }
      if (name === 'react-dom/client') return { createRoot: () => ({ render() {} }) }
      if (name === 'react-router-dom') return { BrowserRouter() {} }
      if (name === './App') return () => null
      if (name === './ui') return { ErrorBanner() {} }
      return require(name)
    },
    fetch: async (url, init) => {
      const request = { url, authorization: new Headers(init.headers).get('Authorization') }
      requests.push(request)
      return handler(request)
    },
  })
  runtime = moduleData.exports
  return {
    ...runtime, requests, localStorage, sessionStorage,
    respond: next => { handler = next },
    login: space => runtime.useAuth.getState().login(space),
    query: space => runtime.queryClient.fetchQuery({ queryKey: keyFor(space), queryFn: () => runtime.api.memories(), retry: false }),
    retry: () => runtime.GlobalErrorBanner().props.children.props.action.props.onClick(),
    cleanup: () => runtime.queryClient.clear(),
  }
}

test('失败查询包含凭证的标识仅留在内存，不写入 localStorage 或 sessionStorage', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    await assert.rejects(s.query(spaceA))
    assert.equal(s.useQueryErrors.getState().failures.length, 1)
    assert.equal(s.localStorage.getItem('mnemotrace.auth'), null)
    assert.equal(s.sessionStorage.getItem('mnemotrace.auth'), null)
    assert.equal(s.sessionStorage.getItem('mnemotrace.query-errors'), null)
    assert.ok(![...s.localStorage.values(), ...s.sessionStorage.values()].some(value => value.includes(spaceA)))
  } finally { s.cleanup() }
})

test('刷新时移除旧查询错误中的凭证，不恢复旧重试入口', () => {
  const s = setup(JSON.stringify([{ key: JSON.stringify(keyFor(spaceA)), message: 'synthetic error' }]))
  try {
    assert.equal(s.useAuth.getState().apiKey, null)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    assert.equal(s.sessionStorage.getItem('mnemotrace.query-errors'), null)
  } finally { s.cleanup() }
})

test('账号切换同步移除旧账号的成功与失败查询，保留公共健康缓存', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    s.queryClient.setQueryData(['profile', spaceA], { private: 'synthetic-a' })
    s.queryClient.setQueryData(['health'], { ok: true })
    await assert.rejects(s.query(spaceA))
    s.login(spaceB)
    assert.ok(!s.queryClient.getQueryCache().find({ queryKey: keyFor(spaceA) }), '旧账号查询必须被移除')
    assert.equal(s.queryClient.getQueryData(['profile', spaceA]), undefined)
    assert.equal(s.queryClient.getQueryData(['health']).ok, true)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
  } finally { s.cleanup() }
})

test('全局重试只使用当前账号的查询，不把当前数据写入其他账号缓存', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    await assert.rejects(s.query(spaceA))
    s.login(spaceB)
    // 即使缓存里残留旧账号查询，重试入口也必须再次验证归属。
    await assert.rejects(s.query(spaceA))
    await assert.rejects(s.query(spaceB))
    assert.equal(s.useQueryErrors.getState().failures.length, 1)
    const before = s.requests.length
    s.respond(async () => response({ user: spaceB, facts: ['synthetic-b-private'] }))
    s.retry()
    await turn()
    assert.equal(s.requests.length - before, 1)
    assert.equal(s.queryClient.getQueryData(keyFor(spaceA)), undefined)
    assert.equal(s.queryClient.getQueryData(keyFor(spaceB)).user, spaceB)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    s.login(spaceA)
    assert.equal(s.queryClient.getQueryData(keyFor(spaceA)), undefined)
  } finally { s.cleanup() }
})

test('退出时取消进行中的查询，旧请求迟到后不恢复缓存或错误提示', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    let finish
    s.respond(() => new Promise(resolve => { finish = resolve }))
    const pending = s.query(spaceA).catch(() => {})
    s.useAuth.getState().logout()
    assert.equal(s.queryClient.isFetching(), 0)
    s.login(spaceB)
    finish(response({ user: spaceA, facts: ['synthetic-a-private'] }))
    await pending
    await turn()
    assert.equal(s.queryClient.getQueryData(keyFor(spaceA)), undefined)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
  } finally { s.cleanup() }
})

test('旧账号迟到的 401 不会退出新账号', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    let finish
    s.respond(() => new Promise(resolve => { finish = resolve }))
    const pending = s.api.memories()
    s.login(spaceB)
    finish({ ok: false, status: 401 })
    await assert.rejects(pending, error => error.status === 409)
    assert.equal(s.useAuth.getState().apiKey, spaceB)
  } finally { s.cleanup() }
})

test('读取响应正文期间切换账号，旧响应也不会作为成功结果返回', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    let finish
    s.respond(async () => ({ ok: true, status: 200, json: () => new Promise(resolve => { finish = resolve }) }))
    const pending = s.api.memories()
    await turn()
    s.login(spaceB)
    finish({ user: spaceA, facts: ['synthetic-a-private'] })
    await assert.rejects(pending, error => error.status === 409)
    assert.equal(s.useAuth.getState().apiKey, spaceB)
  } finally { s.cleanup() }
})

test('原文溯源查询按账号隔离，切换账号后重新请求且退出后禁用', async () => {
  const s = setup()
  try {
    s.respond(async request => response({ episodes: [{ content: request.authorization }] }))
    s.login(spaceA)
    const first = s.useSourceEpisodes(true)
    await s.queryClient.fetchQuery(first)
    s.login(spaceB)
    const second = s.useSourceEpisodes(true)
    assert.notEqual(first.queryKey[1], second.queryKey[1])
    assert.equal(s.queryClient.getQueryData(first.queryKey), undefined)
    const result = await s.queryClient.fetchQuery(second)
    assert.equal(result.episodes[0].content, `Bearer ${spaceB}`)
    assert.equal(s.requests.length, 2)
    s.useAuth.getState().logout()
    assert.equal(s.useSourceEpisodes(true).enabled, false)
  } finally { s.cleanup() }
})

test('登录过程中公共健康请求可以正常完成，不受私有查询隔离影响', async () => {
  const s = setup()
  try {
    let finish
    s.respond(() => new Promise(resolve => { finish = resolve }))
    const pending = s.queryClient.fetchQuery({ queryKey: ['health'], queryFn: s.api.health })
    s.login(spaceA)
    finish(response({ ok: true }))
    assert.equal((await pending).ok, true)
    assert.equal(s.queryClient.getQueryData(['health']).ok, true)
    assert.equal(s.useAuth.getState().apiKey, spaceA)
  } finally { s.cleanup() }
})

test('当前账号查询可正常重试，成功后清除错误；无缓存错误可直接恢复页面', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    await assert.rejects(s.query(spaceA))
    assert.equal(s.useQueryErrors.getState().failures.length, 1)
    s.respond(async () => response({ user: spaceA, facts: [] }))
    s.retry()
    await turn()
    assert.equal(s.queryClient.getQueryData(keyFor(spaceA)).user, spaceA)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    s.useQueryErrors.getState().report('synthetic-stale-error', 'synthetic error')
    const before = s.requests.length
    s.retry()
    assert.equal(s.requests.length, before)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
  } finally { s.cleanup() }
})

test('直接同 Key 登录也同步清空私有缓存和旧错误，保留健康缓存', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    s.queryClient.setQueryData(['profile', spaceA], { private: 'synthetic-a' })
    s.queryClient.setQueryData(['health'], { ok: true })
    await assert.rejects(s.query(spaceA))
    s.login(spaceA)
    assert.equal(s.queryClient.getQueryData(['profile', spaceA]), undefined)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    assert.equal(s.queryClient.getQueryData(['health']).ok, true)
  } finally { s.cleanup() }
})

test('同 Key 新登录取消旧查询，迟到结果不恢复缓存和错误', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    let finish
    s.respond(() => new Promise(resolve => { finish = resolve }))
    const oldQuery = s.query(spaceA).catch(() => {})
    s.login(spaceA)
    assert.equal(s.queryClient.isFetching(), 0)
    finish(response({ user: spaceA, private: 'synthetic-old' }))
    await oldQuery
    await turn()
    assert.equal(s.queryClient.getQueryData(keyFor(spaceA)), undefined)
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    s.respond(async () => response({ user: spaceA, current: true }))
    assert.equal((await s.query(spaceA)).current, true)
  } finally { s.cleanup() }
})

test('取消类型不会进入当前查询的全局错误横幅', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    await assert.rejects(s.queryClient.fetchQuery({
      queryKey: keyFor(spaceA), retry: false,
      queryFn: async () => { throw new s.StaleSessionError() },
    }))
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
  } finally { s.cleanup() }
})


test('分页版本变化交给局部重新加载，同时清除此前网络错误的全局重试入口', async () => {
  const s = setup()
  try {
    s.login(spaceA)
    await assert.rejects(s.query(spaceA))
    assert.equal(s.useQueryErrors.getState().failures.length, 1)
    s.respond(async () => ({ ok: false, status: 409, json: async () => ({ detail: {
      code: 'memory_revision_changed', message: '记录已变化，请重新加载',
    } }) }))
    await assert.rejects(s.query(spaceA), error => error.name === 'MemoryRevisionChangedError' && !(error instanceof s.StaleSessionError))
    assert.equal(s.useQueryErrors.getState().failures.length, 0)
    assert.equal(s.useAuth.getState().apiKey, spaceA)
  } finally { s.cleanup() }
})
