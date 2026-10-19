const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Health.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/Modal', '../components/Toast', '../hooks/queries'],
}).outputFiles[0].text

function setup() {
  const states = [], effects = [], timers = new Map(), requests = []
  let stateIndex = 0, effectIndex = 0, now = 0, timerId = 0, fetching = false, mounted = true, lateUpdates = 0
  const react = {
    ...React,
    useState(initial) {
      const slot = stateIndex++
      if (!(slot in states)) states[slot] = typeof initial === 'function' ? initial() : initial
      return [states[slot], value => {
        if (!mounted) lateUpdates++
        states[slot] = typeof value === 'function' ? value(states[slot]) : value
      }]
    },
    useRef(initial) { return react.useState({ current: initial })[0] },
    useEffect(effect) { const slot = effectIndex++; if (!(slot in effects)) effects[slot] = effect() },
    useLayoutEffect() {},
  }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    performance: { now: () => now },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, at: now + delay }); return id },
    clearTimeout(id) { timers.delete(id) },
    require(name) {
      if (name === 'react') return react
      if (name === '../hooks/queries') return {
        useAudit: () => ({
          data: { checked: { facts: 0, entities: 0 }, total_findings: 0, by_kind: {}, findings: [] },
          isFetching: fetching,
          refetch() {
            fetching = true
            return new Promise(resolve => requests.push(() => { fetching = false; resolve({}) }))
          },
        }),
        useClearSlot: () => ({ isPending: false }),
      }
      if (name === '../components/ui') return Object.fromEntries(['Button', 'Card', 'CardTitle', 'EmptyState', 'ErrorState', 'PageHeader', 'Spinner'].map(key => [key, () => null]))
      if (name === '../components/Modal') return { ConfirmDialog: () => null }
      if (name === '../components/Toast') return { toast: { success() {}, error() {} } }
      return require(name)
    },
  })
  function findAction(node) {
    if (!node || typeof node !== 'object') return
    if (node.props?.hint?.props?.onClick) return node.props.hint
    for (const child of [].concat(node.props?.children ?? [])) { const found = findAction(child); if (found) return found }
  }
  function action() { stateIndex = effectIndex = 0; return findAction(moduleData.exports.default()) }
  return {
    action, requests, timers,
    busy() { const props = action().props; return Boolean(props['aria-busy'] || props.loading) },
    advance(ms) {
      now += ms
      for (const [id, timer] of timers) if (timer.at <= now) { timers.delete(id); timer.callback() }
    },
    unmount() { mounted = false; effects.forEach(cleanup => cleanup?.()) },
    lateUpdates: () => lateUpdates,
  }
}

test('40ms 的快速检查仍保留至少 600ms 加载反馈，不闪一下就结束', async () => {
  const s = setup()
  const pending = s.action().props.onClick()
  s.advance(40)
  s.requests[0]()
  await pending
  assert.equal(s.busy(), true, 'response completed after 40ms; loading feedback must remain visible')
  s.advance(559)
  assert.equal(s.busy(), true)
  s.advance(1)
  assert.equal(s.busy(), false)
})

test('慢请求完成后立即结束反馈，不额外等待一个完整时长', async () => {
  const s = setup()
  const pending = s.action().props.onClick()
  s.advance(1500)
  assert.equal(s.busy(), true)
  s.requests[0]()
  await pending
  assert.equal(s.busy(), false)
  assert.equal(s.timers.size, 0)
})

test('加载时只旋转已有刷新图标，按钮不插入第二个图标，快速重复点击不重复请求', async () => {
  const s = setup()
  const onClick = s.action().props.onClick
  const pending = onClick()
  onClick()
  assert.equal(s.requests.length, 1)
  const button = s.action()
  assert.equal(Boolean(button.props.loading), false)
  assert.equal(button.props.disabled, true)
  const icons = [].concat(button.props.children).filter(child => typeof child === 'object' && child !== null)
  assert.equal(icons.length, 1)
  assert.match(icons[0].props.className, /animate-spin/)
  s.requests[0]()
  await pending
})

test('离开页面会清理反馈计时器，未完成请求也不更新已卸载页面', async () => {
  for (const finishFirst of [true, false]) {
    const s = setup()
    const pending = s.action().props.onClick()
    if (finishFirst) { s.requests[0](); await pending }
    s.unmount()
    if (!finishFirst) { s.requests[0](); await pending }
    s.advance(1000)
    assert.equal(s.timers.size, 0)
    assert.equal(s.lateUpdates(), 0)
  }
})
