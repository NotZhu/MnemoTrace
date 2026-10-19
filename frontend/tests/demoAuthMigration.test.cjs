const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

const bundled = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/store/auth.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['react'],
}).outputFiles[0].text

function loadAuth(saved, { unavailable = false } = {}) {
  const local = new Map(Object.entries(saved ?? {})), session = new Map()
  const writes = []
  const storage = (values, name) => ({
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { writes.push([name, key, value]); values.set(key, value) },
    removeItem: key => values.delete(key),
  })
  const window = {}
  for (const [name, values] of [['localStorage', local], ['sessionStorage', session]]) {
    Object.defineProperty(window, name, { get() {
      if (unavailable) throw new Error('synthetic storage unavailable')
      return storage(values, name)
    } })
  }
  const reload = () => {
    const moduleData = { exports: {} }
    vm.runInNewContext(bundled, { module: moduleData, exports: moduleData.exports, require, window })
    return moduleData.exports
  }
  return { ...reload(), reload, local, session, writes }
}

test('清除当前存储名下的旧凭证，不恢复登录且不影响主题与空间偏好', () => {
  for (const version of [0, 1]) {
    const auth = loadAuth({
      'mnemotrace.auth': JSON.stringify({ state: { apiKey: 'synthetic-private-key' }, version }),
      'mnemotrace.theme': 'synthetic-theme',
      'mnemotrace.preferences': 'synthetic-preferences',
    })
    assert.equal(auth.currentKey(), null)
    assert.equal(auth.local.has('mnemotrace.auth'), false)
    assert.equal(auth.local.get('mnemotrace.theme'), 'synthetic-theme')
    assert.equal(auth.local.get('mnemotrace.preferences'), 'synthetic-preferences')
    assert.equal(auth.writes.length, 0)
  }
})

test('普通与演示 Key 仅在当前页面内存登录，刷新不恢复凭证', () => {
  for (const key of ['test', '1', 'mnemotrace-test', 'synthetic-private-key']) {
    const auth = loadAuth()
    auth.useAuth.getState().login(` ${key} `)
    assert.equal(auth.currentKey(), key)
    assert.equal(auth.writes.length, 0)
    assert.equal(auth.local.has('mnemotrace.auth'), false)
    assert.equal(auth.session.size, 0)
    assert.equal(auth.reload().currentKey(), null)
  }
})

test('退出清除内存凭证，重新登录不写入浏览器存储', () => {
  const auth = loadAuth()
  auth.useAuth.getState().login('synthetic-private-key')
  auth.useAuth.getState().logout()
  assert.equal(auth.currentKey(), null)
  auth.useAuth.getState().login(' mnemotrace-test ')
  assert.equal(auth.currentKey(), 'mnemotrace-test')
  assert.equal(auth.writes.length, 0)
})

test('浏览器禁止存储时仍可登录和退出', () => {
  const auth = loadAuth(undefined, { unavailable: true })
  assert.equal(auth.currentKey(), null)
  auth.useAuth.getState().login('synthetic-private-key')
  assert.equal(auth.currentKey(), 'synthetic-private-key')
  auth.useAuth.getState().logout()
  assert.equal(auth.currentKey(), null)
})
