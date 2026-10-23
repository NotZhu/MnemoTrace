const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Conversations.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/Modal', '../components/Toast', '../hooks/queries'],
}).outputFiles[0].text

function findNode(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of Array.isArray(node) ? node : [].concat(node.props?.children ?? [])) {
    const found = findNode(child, predicate)
    if (found) return found
  }
}

function setup(sessionItems = []) {
  const states = [], effects = [], timers = new Map(), queries = []
  const closed = [], reports = []
  let stateIndex, effectIndex, pendingEffects, now = 0, timerId = 0
  const hookReact = {
    ...React,
    useRef(initial) {return hookReact.useState({current: initial})[0]},
    useState(initial) {
      const index = stateIndex++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value}]
    },
    useEffect(callback, deps) {
      const index = effectIndex++, previous = effects[index]
      if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
        pendingEffects.push(() => {
          previous?.cleanup?.()
          effects[index] = {deps, cleanup: callback()}
        })
      }
    },
  }
  const moduleData = {exports: {}}
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    window: {
      setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id},
      clearTimeout(id) {timers.delete(id)},
    },
    require(name) {
      if (name === 'react') return hookReact
      if (name === '../hooks/queries') return {
        useMemoryPages: () => ({data: {episodes: []}}),
        useAgentStatus: () => ({}), useWorking: () => ({refetch(){}}), useSessionReport: (id) => {reports.push(id);return {}},
        useRemember: () => ({}), useClearWorking: () => ({}),
        useCloseSession: () => ({mutate(id, callbacks){closed.push(id);callbacks.onSuccess({facts_added:1,outcomes:1})}}),
        useSessions(params, options) {queries.push({params, options}); return {data: {sessions: sessionItems}}},
      }
      if (name === '../components/ui') return Object.fromEntries(
        ['Badge', 'Button', 'Card', 'CardTitle', 'EmptyState', 'ErrorState', 'Field', 'PageHeader', 'Spinner'].map(key => [key, () => null]),
      )
      if (name === '../components/Modal') return {Modal: () => null}
      if (name === '../components/Toast') return {toast: {success(){}}}
      return require(name)
    },
  })
  function render() {
    stateIndex = effectIndex = 0
    pendingEffects = []
    const tree = moduleData.exports.default()
    pendingEffects.forEach(callback => callback())
    return tree
  }
  return {
    render, closed, reports,
    input: () => findNode(render(), node => node.type === 'input' && node.props.className.includes('mb-3')),
    tick(ms) {
      now += ms
      for (const [id, timer] of timers) {
        if (timer.at <= now) {timers.delete(id); timer.callback()}
      }
      render()
    },
    query: () => queries.at(-1).params.q,
    options: () => queries.at(-1).options,
  }
}

test('会话列表连续输入停顿 500ms 后才查询，输入框立即更新', () => {
  const s = setup()
  s.input().props.onChange({target: {value: 'demo'}})
  s.render()
  assert.equal(s.input().props.value, 'demo')
  assert.equal(s.query(), '')
  s.tick(300)
  s.input().props.onChange({target: {value: 'demo-session'}})
  s.render()
  s.tick(499)
  assert.equal(s.query(), '')
  s.tick(1)
  assert.equal(s.query(), 'demo-session')
  assert.equal(s.options().keepPreviousData, true)
})

test('会话列表不查询中文组词中的拼音，清空后立即恢复全部会话', () => {
  const s = setup()
  s.input().props.onCompositionStart()
  s.render()
  s.input().props.onChange({target: {value: 'yan'}})
  s.render()
  s.tick(1000)
  assert.equal(s.query(), '')
  s.input().props.onChange({target: {value: '演示'}})
  s.input().props.onCompositionEnd()
  s.render()
  s.tick(500)
  assert.equal(s.query(), '演示')
  s.input().props.onChange({target: {value: ''}})
  s.render()
  s.render()
  assert.equal(s.query(), '')
})

test('结束会话只处理所点卡片的 ID，报告打开相同会话，上方输入保持不变', () => {
  const s=setup([{id:'synthetic-a',episodes:2,facts_added:1},{id:'synthetic-b',episodes:3,facts_added:2}])
  const card=findNode(s.render(),node=>node.type==='li' && node.key==='synthetic-b')
  const finish=findNode(card,node=>node.type==='button' && node.props.children==='结束会话')
  assert.ok(finish)
  finish.props.onClick()
  const tree=s.render()
  assert.deepEqual(s.closed,['synthetic-b'])
  assert.equal(s.reports.at(-1),'synthetic-b')
  const picker=findNode(tree,node=>node.type?.name==='SessionPicker')
  assert.equal(picker.props.value,'default')
})

test('空会话列表不在添加记忆区域提供结束会话入口', () => {
  const s=setup()
  assert.equal(findNode(s.render(),node=>node.props?.title==='整理本次会话，提取长期记忆和会话结论。'),undefined)
})

test('默认使用 default 会话，列表中的使用此会话会同步顶部选择', () => {
  const s=setup([{id:'synthetic-session',episodes:1,facts_added:1}])
  assert.equal(findNode(s.render(),node=>node.type?.name==='SessionPicker').props.value,'default')
  const card=findNode(s.render(),node=>node.type==='li' && node.key==='synthetic-session')
  findNode(card,node=>node.type==='button' && node.props.children==='使用此会话').props.onClick()
  assert.equal(findNode(s.render(),node=>node.type?.name==='SessionPicker').props.value,'synthetic-session')
})
