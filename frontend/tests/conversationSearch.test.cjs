const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Conversations.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  define: {'import.meta.env': '{}'},
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/Modal', '../components/Toast', '../components/OperationProgress', '../hooks/queries'],
}).outputFiles[0].text

function findNode(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of Array.isArray(node) ? node : [].concat(node.props?.children ?? [])) {
    const found = findNode(child, predicate)
    if (found) return found
  }
}

function setup(sessionItems = [], episodes = [], { reportPage } = {}) {
  const states = [], effects = [], timers = new Map(), queries = []
  const reports = [], reportOffsets = [], submitted = []
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
    module: moduleData, exports: moduleData.exports, AbortController,
    crypto: {randomUUID: () => `synthetic-${submitted.length + 1}`},
    window: {
      setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id},
      clearTimeout(id) {timers.delete(id)},
    },
    require(name) {
      if (name === 'react') return hookReact
      if (name === '../hooks/queries') return {
        useMemoryPages: () => ({data: {episodes}}),
        useAgentStatus: () => ({}), useWorking: () => ({refetch(){}}),
        useSessionReport: (id, _, offset) => {
          reports.push(id); reportOffsets.push(offset)
          return reportPage && id ? {data: reportPage(offset)} : {}
        },
        useRemember: () => ({mutate(input, callbacks) {submitted.push({input, callbacks})}}), useClearWorking: () => ({}),
        useSessions(params, options) {queries.push({params, options}); return {data: {sessions: sessionItems}}},
      }
      if (name === '../components/ui') return Object.fromEntries(
        ['Badge', 'Button', 'Card', 'CardTitle', 'EmptyState', 'ErrorState', 'Field', 'PageHeader', 'Spinner'].map(key => [key, () => null]),
      )
      if (name === '../components/Modal') return {Modal: () => null}
      if (name === '../components/OperationProgress') return {OperationProgress: () => null}
      if (name === '../components/Toast') return {toast: {success(){}, info(){}, error(){}}}
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
    render, reports, reportOffsets, submitted,
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

test('对话重试保持消息 ID，角色属于不可变输入，成功后可再次写相同正文', () => {
  const s = setup()
  const text = () => findNode(s.render(), node => node.type === 'textarea')
  const send = () => text().props.onKeyDown({ctrlKey: true, key: 'Enter'})
  text().props.onChange({target: {value: 'Synthetic dialogue'}})
  send()
  s.submitted[0].callbacks.onError(new Error('synthetic timeout'))
  send()
  assert.equal(s.submitted[0].input.value[0].message_id, s.submitted[1].input.value[0].message_id)
  const role = findNode(s.render(), node => node.type === 'select' && node.props['aria-label'] === '消息角色')
  role.props.onChange({target: {value: 'assistant'}})
  send()
  assert.notEqual(s.submitted[1].input.value[0].message_id, s.submitted[2].input.value[0].message_id)
  assert.equal(s.submitted[2].input.value[0].role, 'assistant')
  s.submitted[2].callbacks.onSuccess({status: "accepted"})
  text().props.onChange({target: {value: 'Synthetic dialogue'}})
  send()
  assert.notEqual(s.submitted[2].input.value[0].message_id, s.submitted[3].input.value[0].message_id)
})

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

test('报告打开所点会话，上方输入会话保持不变', () => {
  const s = setup([{session_id:'synthetic-a',episodes:2}, {session_id:'synthetic-b',episodes:3}])
  const card = findNode(s.render(), node => node.type === 'li' && node.key === 'synthetic-b')
  findNode(card, node => node.type === 'button' && node.props.children === '查看报告').props.onClick()
  const tree = s.render()
  assert.equal(s.reports.at(-1), 'synthetic-b')
  assert.equal(findNode(tree, node => node.type?.name === 'SessionPicker').props.value, 'default')
})

test('会话报告使用服务端的下一页偏移，新会话报告从第一页开始', () => {
  const s = setup([{session_id:'synthetic-a',episodes:1,facts_added:101}], [], {reportPage: offset => ({
    facts: [], facts_page: {has_more: offset === 0, next_offset: offset === 0 ? 100 : null},
  })})
  const open = () => findNode(s.render(), node => node.type==='button' && node.props.children==='查看报告')
  open().props.onClick()
  findNode(s.render(), node => node.props?.children==='下一页').props.onClick()
  s.render()
  assert.equal(s.reportOffsets.at(-1), 100)
  open().props.onClick()
  s.render()
  assert.equal(s.reportOffsets.at(-1), 0)
})

test('默认使用 default 会话，列表中的使用此会话会同步顶部选择', () => {
  const s=setup([{session_id:'synthetic-session',episodes:1,facts_added:1}])
  assert.equal(findNode(s.render(),node=>node.type?.name==='SessionPicker').props.value,'default')
  const card=findNode(s.render(),node=>node.type==='li' && node.key==='synthetic-session')
  findNode(card,node=>node.type==='button' && node.props.children==='使用此会话').props.onClick()
  assert.equal(findNode(s.render(),node=>node.type?.name==='SessionPicker').props.value,'synthetic-session')
})


test('列表显示已有摘要与原文，没有摘要时仍可浏览原文', () => {
  const s = setup([], [
    {id:'one', content:'完整原文仍在', date:'2026-10-31', session_id:'s', digest:''},
    {id:'two', content:'另一条完整原文', date:'2026-10-31', session_id:'s', digest:'已保存摘要',
      coverage:{partial:false}, digest_coverage:{partial:true}},
  ])
  const tree = s.render()
  assert.ok(findNode(tree, node => node.type === 'p' && node.props.children === '完整原文仍在'))
  const digest = findNode(tree, node => node.type === 'p' && node.props.children.includes('已保存摘要'))
  assert.ok(findNode(digest, node => node.type === 'span' && node.props.children === '（仅覆盖部分原文）'))
})
