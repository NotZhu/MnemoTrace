const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const React = require('react')
const esbuild = require('esbuild')

const source = esbuild.buildSync({
  stdin: {
    contents: "export { default as Privacy } from './pages/Privacy'; export { api } from './lib/api'; export * from './store/auth'",
    resolveDir: path.resolve(__dirname, '../src'), loader: 'tsx',
  },
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/Modal',
    '../components/ExportRangeSelect', '../components/Toast', '../hooks/queries'],
  define: { 'import.meta.env.VITE_API_BASE': '""' },
}).outputFiles[0].text
const appSource = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/App.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'react-router-dom', './components/Layout', './store/auth', './pages/*'],
}).outputFiles[0].text

function setup(mode = 'compact') {
  let handler, stateIndex = 0
  const calls = { toast: [], exporting: [], downloads: 0 }
  const Button = () => null
  const react = { ...React, useEffect() {}, useDebugValue() {}, useCallback: fn => fn,
    useSyncExternalStore: (_subscribe, snapshot) => snapshot(),
    useState(initial) {
      const index = stateIndex++
      return [initial === 'compact' ? mode : initial, value => { if (index === 0) calls.exporting.push(value) }]
    },
  }
  const modules = {
    react,
    'lucide-react': { Download() {}, ShieldCheck() {}, Trash2() {} },
    '../components/ui': { Button, Card() {}, CardTitle() {}, PageHeader() {} },
    '../components/Modal': { ConfirmDialog() {} },
    '../components/ExportRangeSelect': { ExportRangeSelect() {} },
    '../components/Toast': { toast: {
      success: message => calls.toast.push({ kind: 'success', message }),
      error: message => calls.toast.push({ kind: 'error', message }),
    } },
    '../hooks/queries': { useAgentStatus: () => ({ data: { user: 'synthetic-user' } }), useForget: () => ({}) },
  }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports, Headers, URLSearchParams, AbortController, AbortSignal,
    require: name => modules[name] ?? require(name),
    fetch: (...args) => handler(...args),
    URL: { createObjectURL: () => 'blob:synthetic', revokeObjectURL() {} },
    document: { body: { appendChild() {} }, createElement: () => ({ click() { calls.downloads++ }, remove() {} }) },
  })
  const runtime = moduleData.exports
  function find(node) {
    if (!node || typeof node !== 'object') return null
    if (node.type === Button && node.props.loading !== undefined) return node
    for (const child of [node.props?.children].flat(Infinity)) {
      const result = find(child)
      if (result) return result
    }
    return null
  }
  return {
    ...runtime, calls,
    login: key => { runtime.useAuth.getState().login(key); runtime.useAuth.getState().selectSpace('space-test') },
    logout: () => runtime.useAuth.getState().logout(),
    respond: next => { handler = next },
    export: () => { stateIndex = 0; return find(runtime.Privacy()).props.onClick() },
    app() {
      const mod = { exports: {} }
      vm.runInNewContext(appSource, {
        module: mod, exports: mod.exports,
        require(name) {
          if (name === 'react') return react
          if (name === './store/auth') return runtime
          if (name === 'react-router-dom') return { Routes() {}, Route() {}, Navigate() {} }
          if (name.startsWith('./pages/') || name === './components/Layout') return () => null
          return require(name)
        },
      })
      return mod.exports.default()
    },
  }
}

for (const outcome of ['success', 'http-error', 'network-error']) {
  test(`Privacy discards old ${outcome} without toast or state updates after relogin`, async () => {
    const s = setup()
    s.login('synthetic-A')
    let finish, fail
    s.respond(() => new Promise((resolve, reject) => { finish = resolve; fail = reject }))
    const pending = s.export()
    s.logout()
    s.login('synthetic-A')
    if (outcome === 'network-error') fail(new TypeError('synthetic network failure'))
    else finish({ ok: outcome === 'success', status: outcome === 'success' ? 200 : 500,
      blob: async () => new Blob(['synthetic memory']) })
    await pending
    assert.equal(s.calls.downloads, 0)
    assert.deepEqual(s.calls.toast, [])
    assert.deepEqual(s.calls.exporting, [true])
  })
}

test('Privacy rechecks the session between API completion and its success callback', async () => {
  const s = setup()
  s.login('synthetic-A')
  s.respond(async () => ({ ok: true, status: 200, blob: async () => new Blob(['synthetic']) }))
  const original = s.api.exportDownload
  s.api.exportDownload = (...args) => {
    const result = original(...args)
    // 先登记此回调，精确安排在 API 完成之后、Privacy await 恢复之前。
    void result.then(() => s.login('synthetic-A'), () => {})
    return result
  }
  await s.export()
  assert.equal(s.calls.downloads, 1, '已触发的下载不追溯撤销')
  assert.deepEqual(s.calls.toast, [])
  assert.deepEqual(s.calls.exporting, [true])
})

for (const mode of ['compact', 'with-originals']) {
  test(`Privacy ${mode} succeeds normally and shows one success toast`, async () => {
    const s = setup(mode)
    s.login('synthetic-A')
    s.respond(async url => {
      assert.equal(new URL(url, 'http://test').searchParams.get('include_sensitive'), String(mode === 'with-originals'))
      return { ok: true, status: 200, blob: async () => new Blob(['synthetic']) }
    })
    await s.export()
    assert.equal(s.calls.downloads, 1)
    assert.equal(s.calls.toast.length, 1)
    assert.equal(s.calls.toast[0].kind, 'success')
    assert.deepEqual(s.calls.exporting, [true, false])
  })
}

test('Privacy reports current-session export failures', async () => {
  const s = setup()
  s.login('synthetic-A')
  s.respond(async () => ({ ok: false, status: 500 }))
  await s.export()
  assert.equal(s.calls.downloads, 0)
  assert.equal(s.calls.toast[0].kind, 'error')
  assert.deepEqual(s.calls.exporting, [true, false])
})

test('same-key login changes the authenticated layout key, resetting the old page tree', () => {
  const s = setup()
  s.login('synthetic-A')
  const oldKey = s.app().props.children[0].props.element.key
  s.login('synthetic-A')
  const newKey = s.app().props.children[0].props.element.key
  assert.notEqual(oldKey, newKey)
})
