const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

const message = 'API Key 只能使用英文、数字和半角符号，不能包含中文、全角字符或换行。请重新输入。'
const bundle = (file, options = {}) => esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, file)],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  ...options,
}).outputFiles[0].text
const apiSource = bundle('../src/lib/api.ts', {
  external: ['../store/auth'], define: { 'import.meta.env.VITE_API_BASE': '""' },
})
const authSource = bundle('../src/store/auth.ts', { external: ['react'] })

function client(key) {
  let requests = 0
  let loggedOut = false
  let authorization
  let lastRequest
  const moduleData = { exports: {} }
  vm.runInNewContext(apiSource, {
    module: moduleData, exports: moduleData.exports, Headers, URLSearchParams, AbortController, AbortSignal,
    require: name => {
      assert.equal(name, '../store/auth')
      return { currentSession: () => ({ key, epoch: 1, spaceId: key ? 'space-test' : null }),
        isCurrentSession: session => session.key === key && session.epoch === 1,
        useAuth: { subscribe() {}, getState: () => ({ logout: () => { loggedOut = true } }) } }
    },
    fetch: async (url, init) => {
      lastRequest = { url, init }
      // Native Headers validates the export path too, just as browser fetch does.
      authorization = new Headers(init.headers).get('Authorization')
      requests++
      return { ok: true, status: 200, json: async () => ({ ok: true }) }
    },
  })
  return { api: moduleData.exports.api, requests: () => requests,
    loggedOut: () => loggedOut, authorization: () => authorization, lastRequest: () => lastRequest }
}

test('召回请求保留调用方的问答开关', async () => {
  const c = client('synthetic-key')
  for (const answer of [true, false]) {
    await c.api.recall('Synthetic question', { answer })
    assert.equal(c.lastRequest().url, '/v1/recall')
    assert.equal(JSON.parse(c.lastRequest().init.body).answer, answer)
  }
})

test('画像刷新发送空间请求体，读取画像使用缓存接口', async () => {
  const c = client('synthetic-key')
  await c.api.refreshProfile()
  assert.equal(c.lastRequest().url, '/v1/profile/refresh')
  assert.equal(c.lastRequest().init.method, 'POST')
  assert.deepEqual(JSON.parse(c.lastRequest().init.body), {space_id: 'space-test'})
  await c.api.profile()
  assert.equal(c.lastRequest().url, '/v1/profile?space_id=space-test')
})

function hydrate(key, version = 1) {
  let saved = JSON.stringify({ state: { apiKey: key }, version })
  const localStorage = {
    getItem: () => saved, setItem: (_, value) => { saved = value }, removeItem: () => { saved = null },
  }
  const moduleData = { exports: {} }
  vm.runInNewContext(authSource, {
    module: moduleData, exports: moduleData.exports, require, window: { localStorage },
  })
  return { ...moduleData.exports, saved: () => JSON.parse(saved) }
}

test('中文 Key 在普通请求和导出前返回中文错误，不触发 Headers 异常或网络请求', async () => {
  for (const method of ['health', 'exportDownload']) {
    const c = client('合成空间')
    await assert.rejects(c.api[method](), { name: 'ApiError', status: 400, message })
    assert.equal(c.requests(), 0)
    assert.equal(c.loggedOut(), true)
  }
})

test('ASCII Key 原样发送；未登录仍能请求健康状态', async () => {
  for (const key of ['synthetic-key_123', null]) {
    const c = client(key)
    await c.api.health()
    assert.equal(c.authorization(), key ? `Bearer ${key}` : null)
    assert.equal(c.requests(), 1)
    assert.equal(c.loggedOut(), false)
  }
})

test('登录拒绝中文、全角、换行及空 Key，不持久化无效凭证', () => {
  for (const key of ['合成空间', 'ｓｙｎｔｈｅｔｉｃ', 'synthetic\nkey', 'synthetic\tkey', '']) {
    const auth = hydrate(null)
    auth.useAuth.getState().login(key)
    assert.equal(auth.currentKey(), null)
    assert.equal(auth.useAuth.getState().keyError, message)
    assert.equal(auth.saved(), null)
  }
})

test('旧凭证缓存只清除不恢复，重新输入有效 Key 后仅在内存登录', () => {
  for (const version of [0, 1]) {
    const auth = hydrate('合成空间', version)
    assert.equal(auth.currentKey(), null)
    assert.equal(auth.useAuth.getState().keyError, null)
    assert.equal(auth.saved(), null)
    auth.useAuth.getState().login(' synthetic-key ')
    assert.equal(auth.currentKey(), 'synthetic-key')
    assert.equal(auth.useAuth.getState().keyError, null)
    assert.equal(auth.saved(), null)
  }
})
