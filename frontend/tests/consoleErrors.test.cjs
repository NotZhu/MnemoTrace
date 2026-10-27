const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const React = require('react')
const esbuild = require('esbuild')

// Component reconciliation with state/effect lifetimes. Network/query states are
// controlled here; the real DOM + QueryClient integration is exercised in browser.
function setup(pageName) {
  const source = esbuild.buildSync({
    entryPoints: [path.resolve(__dirname, `../src/pages/${pageName}.tsx`)],
    bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
    external: ['react', 'react/jsx-runtime', 'lucide-react', 'react-router-dom', '../components/*', '../hooks/queries', '../store/preferences', '../lib/api'],
  }).outputFiles[0].text
  const fact = { id: 'synthetic-fact', subject: 'synthetic-session', predicate: 'decision', object: 'synthetic original', text: 'synthetic original', display: 'synthetic original', status: 'live', valid_at: '2026-10-22', provenance: ['synthetic-episode'], source: 'extracted', topic: 'food', topic_source: 'rule' }
  const finding = { kind: 'fragment', fact_id: fact.id, ...fact }
  const results = {
    usePolicy: { data: { policy: { extract_instruction: '' }, defaults: { extract_system: 'synthetic', summary_system: 'synthetic', persona_system: 'synthetic' } } },
    useFocus: { data: { track: ['synthetic'], mute: [] } },
    useMemoryPages: { data: { facts: [fact], episodes: [], facts_page: { total: 1 }, fact_categories: [] } },
    useMemories: {}, useStructuredProfile: {},
    useAudit: { data: { checked: { facts: 1, entities: 0 }, total_findings: 1, by_kind: { fragment: 1 }, findings: [finding] } },
    useSessions: { data: { sessions: [] } }, useHealth: { data: { ready: true } },
    useAgentStatus: { data: { user: 'synthetic', counts: { facts_live: 1 }, session: { episodes: 1, episodes_pending: 0 } } },
    useWorking: { data: { items: [] } }, useSessionReport: {}, useSourceEpisodes: {}, useConflicts: { data: { conflicts: [] } },
  }
  let active, hookIndex, nextId = 0, tree
  const instances = new Map(), pendingEffects = [], retries = [], mutations = [], toasts = []
  const react = { ...React,
    useState(initial) {
      const instance = active, slot = hookIndex++
      if (!(slot in instance.states)) instance.states[slot] = typeof initial === 'function' ? initial() : initial
      return [instance.states[slot], value => { instance.states[slot] = typeof value === 'function' ? value(instance.states[slot]) : value }]
    },
    useRef(initial) { return react.useState({ current: initial })[0] },
    useEffect(callback, deps = []) {
      const instance = active, slot = hookIndex++, previous = instance.states[slot]
      if (!previous || deps.some((dep, index) => !Object.is(dep, previous.deps[index]))) {
        instance.states[slot] = { deps }
        pendingEffects.push(callback)
      }
    },
    useLayoutEffect() {}, useId() { return react.useState(() => `synthetic-${++nextId}`)[0] },
  }
  const components = new Map()
  const component = name => {
    if (!components.has(name)) components.set(name, Object.defineProperty(function(props) {
      if (name === 'Modal' && !props.open) return null
      return React.createElement(name, props, props.children, props.footer, props.hint, props.action)
    }, 'name', { value: name }))
    return components.get(name)
  }
  const modules = { exports: {} }
  const queries = new Proxy({}, { get: (_, name) => (...args) => {
    if (!(name in results)) return { isPending: false, mutate: (...args) => mutations.push({ name, args }) }
    const result = typeof results[name] === 'function' ? results[name](...args) : results[name]
    return { ...result, refetch: result.refetch ?? (() => retries.push(name)) }
  } })
  vm.runInNewContext(source, { module: modules, exports: modules.exports, console, performance, setTimeout, clearTimeout,
    window: { setTimeout, clearTimeout, location: { origin: 'http://synthetic' } },
    document: { addEventListener() {}, removeEventListener() {} },
    crypto: require('node:crypto').webcrypto,
    require(name) {
      if (name === 'react') return react
      if (name === 'react-router-dom') return { Link: component('Link') }
      if (name === 'lucide-react') return new Proxy({}, { get: () => () => null })
      if (name === '../hooks/queries') return queries
      if (name === '../lib/api') return { apiBaseUrl: () => 'http://synthetic' }
      if (name === '../store/preferences') return { useSpacePreference: (_page, field) => react.useState(field === 'status' ? 'live' : field === 'category' || field === 'topic' ? '' : false) }
      if (name === '../components/Toast') return { toast: { success: text => toasts.push(text), error() {}, info() {} } }
      if (name.startsWith('../components/')) return new Proxy({}, { get: (_, key) => key === 'cx' ? (...args) => args.filter(Boolean).join(' ') : component(key) })
      return require(name)
    },
  })
  function reconcile(element, key, live) {
    if (element == null || typeof element === 'boolean') return null
    if (Array.isArray(element)) return element.map((child, index) => reconcile(child, `${key}/${child?.key ?? index}`, live))
    if (typeof element !== 'object') return element
    if (typeof element.type === 'function') {
      let instance = instances.get(key)
      if (!instance || instance.type !== element.type) instances.set(key, instance = { id: ++nextId, type: element.type, states: [] })
      live.add(key); active = instance; hookIndex = 0
      const rendered = element.type(element.props)
      return reconcile(rendered, `${key}/render`, live)
    }
    return { ...element, props: { ...element.props, children: reconcile(element.props.children, `${key}/children`, live) } }
  }
  function render() {
    for (let pass = 0; pass < 3; pass++) {
      const live = new Set()
      tree = reconcile(React.createElement(modules.exports.default), 'root', live)
      for (const key of instances.keys()) if (!live.has(key)) instances.delete(key)
      pendingEffects.splice(0).forEach(effect => effect())
    }
    return tree
  }
  function nodes(node = render()) {
    if (node == null || typeof node !== 'object') return []
    return Array.isArray(node) ? node.flatMap(child => nodes(child)) : [node, ...nodes(node.props?.children)]
  }
  return { results, retries, mutations, toasts, render, nodes,
    find: predicate => nodes().find(predicate),
    instance: name => [...instances.values()].filter(i => i.type.name === name).map(i => i.id).join(','),
  }
}

test('Facts 请求失败使用共享错误条的重新加载按钮，只重试事实请求', () => {
  const s = setup('Facts')
  Object.assign(s.results.useMemoryPages, { isError: true, isFetching: true, error: new Error('synthetic facts failure') })
  const error = s.find(n => n.type === 'ErrorState' && n.props.message.includes('synthetic facts failure'))
  assert.ok(error)
  assert.equal(error.props.isRetrying, true)
  error.props.onRetry()
  assert.deepEqual(s.retries, ['useMemoryPages'])
})

test('Conversations 会话状态提示在卡片上方，其他各条读取错误和独立重试保留', () => {
  const s = setup('Conversations')
  s.find(n => n.type === 'textarea').props.onChange({ target: { value: 'synthetic preserved draft' } })
  for (const name of ['useMemoryPages', 'useAgentStatus', 'useWorking', 'useSessions']) {
    Object.assign(s.results[name], { isError: true, error: new Error('synthetic failure') })
  }
  const nodes = s.nodes(), errors = nodes.filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 4, '每条错误独立保留，不合并')
  const statusError = errors.find(n => n.props.message.startsWith('会话状态读取失败'))
  assert.ok(nodes.indexOf(statusError) < nodes.findIndex(n => n.type === 'Card'))
  statusError.props.onRetry()
  assert.deepEqual(s.retries, ['useAgentStatus'])
  assert.ok(s.find(n => n.type === 'textarea' && n.props.value === 'synthetic preserved draft'))
})

test('Settings 接入状态错误放在页面最上方，策略和会话索引错误分别保留', () => {
  const s = setup('Settings')
  for (const name of ['usePolicy', 'useAgentStatus', 'useSessions']) {
    Object.assign(s.results[name], { isError: true, error: new Error('synthetic failure') })
  }
  const nodes = s.nodes(), errors = nodes.filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 3, '不合并或隐藏其他错误')
  const statusError = errors.find(n => n.props.message.startsWith('接入状态读取失败'))
  assert.ok(nodes.indexOf(statusError) < nodes.findIndex(n => n.props?.id === 'codex-setup-heading'), '状态提示替代通用提示，位于接入标题之前')
  assert.ok(nodes.indexOf(statusError) < nodes.findIndex(n => n.type === 'Card'))
  statusError.props.onRetry()
  assert.deepEqual(s.retries, ['useAgentStatus'])
})

for (const failed of [['useMemoryPages', 'useMemories'], ['useMemoryPages'], ['useMemories']]) {
  test(`Journal ${failed.join(' + ')} 失败时只有一条错误，重试所有失败请求`, () => {
    const s = setup('Journal')
    s.find(n => n.type === 'button' && n.props['aria-label'] === '编辑').props.onClick()
    s.find(n => n.type === 'textarea').props.onChange({ target: { value: 'synthetic retained draft' } })
    for (const query of failed) Object.assign(s.results[query], { isError: true, error: new Error('请求失败 (500)') })
    const errors = s.nodes().filter(n => n.type === 'ErrorState')
    assert.equal(errors.length, 1)
    assert.equal(errors[0].props.message.split('请求失败 (500)').length, 2, '同一错误消息只出现一次')
    errors[0].props.onRetry()
    assert.deepEqual(s.retries, failed)
    assert.ok(s.find(n => n.type === 'textarea' && n.props.value === 'synthetic retained draft'))
  })
}

for (const empty of [false, true]) {
  for (const cached of [false, true]) {
    test(`Journal 历史会话失败可独立重试并保留缓存，空结论=${empty}，缓存=${cached}`, () => {
      const s = setup('Journal')
      if (empty) s.results.useMemoryPages.data.facts = []
      s.results.useSessions = {
        isError: true, isFetching: true, error: new Error('synthetic sessions failure'),
        data: cached ? { sessions: [{ id: 'synthetic-history' }] } : undefined,
      }
      const errors = s.nodes().filter(n => n.type === 'ErrorState')
      assert.equal(errors.length, 1)
      assert.equal(errors[0].props.message, '历史会话读取失败：synthetic sessions failure')
      assert.equal(errors[0].props.isRetrying, true)
      assert.equal(!!s.find(n => n.type === 'li' && n.key === 'synthetic-history'), cached)
      errors[0].props.onRetry()
      assert.deepEqual(s.retries, ['useSessions'])

      s.results.useSessions = { data: { sessions: [] } }
      assert.equal(s.nodes().filter(n => n.type === 'ErrorState').length, 0)
      assert.ok(!s.find(n => n.type === 'li' && n.key === 'synthetic-history'))
    })
  }
}

test('Journal 两个不同错误合并后仍能看到各自原因', () => {
  const s = setup('Journal')
  s.results.useMemoryPages = { isError: true, error: new Error('synthetic list failure') }
  s.results.useMemories = { isError: true, error: new Error('synthetic open failure') }
  const errors = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 1)
  assert.match(errors[0].props.message, /synthetic list failure/)
  assert.match(errors[0].props.message, /synthetic open failure/)
  s.results.useMemoryPages = { data: { facts: [], episodes: [] } }
  const remaining = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(remaining.length, 1)
  assert.doesNotMatch(remaining[0].props.message, /synthetic list failure/)
  remaining[0].props.onRetry()
  assert.deepEqual(s.retries, ['useMemories'])
  s.results.useMemories = { data: { facts: [] } }
  assert.equal(s.nodes().filter(n => n.type === 'ErrorState').length, 0)
})

test('Dashboard 首次全部失败只显示一条错误，一次重试三个失败请求', () => {
  const s = setup('Dashboard')
  s.results.useMemories = params => ({ isError: true, error: new Error('请求失败 (500)'),
    refetch: () => s.retries.push(params.kind === 'outcomes' ? 'outcomes' : 'memories') })
  s.results.useStructuredProfile = { isError: true, error: new Error('请求失败 (500)') }
  const errors = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 1)
  assert.equal(errors[0].props.message, '请求失败 (500)')
  errors[0].props.onRetry()
  assert.deepEqual(s.retries, ['memories', 'outcomes', 'useStructuredProfile'])
})

test('Dashboard 辅助请求失败可重试，后台主请求失败不销毁输入草稿', () => {
  const s = setup('Dashboard')
  s.results.useMemories = params => params.kind === 'outcomes'
    ? { isError: true, error: new Error('synthetic outcomes failure'), refetch: () => s.retries.push('outcomes') }
    : { data: { counts: { facts_live: 1, facts_outcomes: 0, facts_superseded: 0, episodes: 0 } } }
  s.results.useStructuredProfile = { isError: true, error: new Error('synthetic profile failure') }
  s.find(n => n.type === 'textarea').props.onChange({ target: { value: 'synthetic dashboard draft' } })
  const errors = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 1)
  assert.match(errors[0].props.message, /synthetic outcomes failure/)
  assert.match(errors[0].props.message, /synthetic profile failure/)
  errors[0].props.onRetry()
  assert.deepEqual(s.retries, ['outcomes', 'useStructuredProfile'])
  const previous = s.results.useMemories
  s.results.useMemories = params => ({ ...previous(params), isError: true, error: new Error('synthetic refresh failure') })
  assert.ok(s.find(n => n.type === 'textarea' && n.props.value === 'synthetic dashboard draft'))
  assert.equal(s.nodes().filter(n => n.type === 'ErrorState').length, 1)
})

test('Profile 主请求或辅助请求失败均可见，并只重试失败请求', () => {
  const s = setup('Profile')
  const profile = { basic: [], preferences: {}, habits: [], tentative: [], counts: { basic: 0, preferences: 0, habits: 0, tentative: 0 } }
  s.results.useStructuredProfile = { data: profile }
  s.results.useMemories = { isError: true, error: new Error('synthetic memories failure') }
  let errors = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 1)
  errors[0].props.onRetry()
  assert.deepEqual(s.retries, ['useMemories'])
  s.results.useStructuredProfile = { isError: true, error: new Error('请求失败 (500)') }
  s.results.useMemories = { isError: true, error: new Error('请求失败 (500)') }
  errors = s.nodes().filter(n => n.type === 'ErrorState')
  assert.equal(errors.length, 1)
  assert.equal(errors[0].props.message, '请求失败 (500)')
  errors[0].props.onRetry()
  assert.deepEqual(s.retries.slice(1), ['useStructuredProfile', 'useMemories'])
})

for (const [action, fields] of [['keep', {}], ['food', { topic: 'food' }], ['unknown', { topic: 'unknown' }], ['reset', { reset_topic: true }]]) {
  test(`Facts 主题操作 ${action} 仅发送用户明确操作的主题字段，失败保留选择`, () => {
    const s = setup('Facts')
    s.find(n => n.type === 'button' && n.props['aria-label'] === '编辑').props.onClick()
    const editor = () => s.find(n => n.type === 'TopicEditor')
    assert.equal(editor().props.current.topic_source, 'rule')
    editor().props.onChange(action)
    s.find(n => n.type === 'Button' && n.props.loading !== undefined).props.onClick()
    const mutation = s.mutations.find(m => m.name === 'useEditFact')
    const patch = JSON.parse(JSON.stringify(mutation.args[0].patch))
    assert.deepEqual(Object.fromEntries(Object.entries(patch).filter(([key]) => key.startsWith('topic') || key === 'reset_topic')), fields)
    mutation.args[1].onError(new Error('synthetic write failure'))
    assert.equal(editor().props.value, action)
    mutation.args[1].onSuccess({ source: 'extracted' })
    assert.equal(s.toasts.at(-1), '已更新')
  })
}

for (const [action, fields] of [['unknown', { topic: 'unknown' }], ['reset', { reset_topic: true }]]) {
  test(`Journal 仅修改主题 ${action} 时不发送正文，失败后保留草稿`, () => {
    const s = setup('Journal')
    s.find(n => n.type === 'button' && n.props['aria-label'] === '编辑').props.onClick()
    const editor = () => s.find(n => n.type === 'TopicEditor')
    editor().props.onChange(action)
    s.find(n => n.type === 'button' && n.props['aria-label'] === '保存').props.onClick()
    const mutation = s.mutations.find(m => m.name === 'useEditFact')
    assert.deepEqual(JSON.parse(JSON.stringify(mutation.args[0].patch)), fields)
    mutation.args[1].onError(new Error('synthetic write failure'))
    mutation.args[1].onSettled()
    assert.equal(editor().props.value, action)
    assert.ok(s.find(n => n.type === 'textarea' && n.props.value === 'synthetic original'))
    s.find(n => n.type === 'button' && n.props['aria-label'] === '保存').props.onClick()
    s.mutations.at(-1).args[1].onSuccess({ source: 'extracted' })
    assert.equal(s.toasts.at(-1), '主题已保存')
  })
}

test('设置草稿在已有策略刷新失败和恢复后保持，独立接入卡片不消失', () => {
  const s = setup('Settings'), input = () => s.find(n => n.type === 'textarea' && n.props.placeholder)
  input().props.onChange({ target: { value: 'synthetic unsaved draft' } })
  s.render()
  const identity = s.instance('CodexSetupCard')
  s.results.usePolicy.isError = true; s.results.usePolicy.error = new Error('synthetic policy failure')
  assert.equal(input()?.props.value, 'synthetic unsaved draft')
  assert.equal(s.instance('CodexSetupCard'), identity)
  s.results.usePolicy.isError = false
  s.results.usePolicy.data = { ...s.results.usePolicy.data, policy: { extract_instruction: 'synthetic server update' } }
  assert.equal(input().props.value, 'synthetic unsaved draft')
})

test('策略首次失败保留策略标题、明确失败原因和重试，不显示未加载的可编辑表单', () => {
  const s = setup('Settings')
  s.results.usePolicy = { isError: true, error: new Error('synthetic failure') }
  assert.ok(s.find(n => n.type === 'input' && n.props.value?.startsWith('codex:')))
    assert.ok(s.find(n => n.type === 'h2' && n.props.id === 'memory-policy-heading'))
  const error = s.find(n => n.type === 'ErrorState')
    assert.equal(error.props.message, '策略读取失败：synthetic failure')
    assert.equal(s.find(n => n.type === 'textarea' && n.props.placeholder), undefined)
  assert.equal(typeof error?.props.onRetry, 'function')
  error.props.onRetry()
  assert.deepEqual(s.retries, ['usePolicy'])
})

test('会话输入不依赖历史查询成功，失败及重试保留正文和会话选择', () => {
  const s = setup('Conversations'), input = () => s.find(n => n.type === 'textarea')
  input().props.onChange({ target: { value: 'synthetic conversation draft' } })
  s.find(n => n.type === 'SessionPicker').props.onChange('synthetic-selected')
  s.results.useMemoryPages = { isError: true, error: new Error('synthetic history failure') }
  assert.equal(input()?.props.value, 'synthetic conversation draft')
  assert.equal(s.find(n => n.type === 'SessionPicker').props.value, 'synthetic-selected')
  s.results.useMemoryPages = { isLoading: true }
  assert.equal(input()?.props.value, 'synthetic conversation draft')
})

for (const [page, query, editor] of [['Journal', 'useMemoryPages', 'OutcomeRow'], ['Health', 'useAudit', 'FindingRow']]) {
  test(`${page} 后台刷新失败保留正在编辑的行和草稿`, () => {
    const s = setup(page)
    const button = s.find(n => n.type === 'button' && ['编辑', '编辑这条记忆'].includes(n.props['aria-label']))
    assert.ok(button, 'edit action exists')
    button.props.onClick()
    const input = () => s.find(n => n.type === 'textarea' || (n.type === 'input' && n.props.value === 'synthetic original'))
    assert.ok(input(), 'editor exists')
    input().props.onChange({ target: { value: 'synthetic row draft' } })
    s.render(); const identity = s.instance(editor)
    s.results[query].isError = true; s.results[query].error = new Error('synthetic refresh failure')
    assert.ok(s.find(n => (n.type === 'textarea' || n.type === 'input') && n.props.value === 'synthetic row draft'))
    assert.equal(s.instance(editor), identity)
    s.results[query].isError = false
    assert.ok(s.find(n => (n.type === 'textarea' || n.type === 'input') && n.props.value === 'synthetic row draft'))
  })
}

test('结论重试恢复后新增、拆分或合并分组不重建已有编辑行', () => {
  const s = setup('Journal')
  const original = s.results.useMemoryPages.data.facts[0]
  s.find(n => n.type === 'button' && n.props['aria-label'] === '编辑').props.onClick()
  s.find(n => n.type === 'textarea').props.onChange({ target: { value: 'synthetic reordered draft' } })
  s.render()
  const identity = s.instance('OutcomeRow')
  const retained = () => {
    assert.ok(s.find(n => n.type === 'textarea' && n.props.value === 'synthetic reordered draft'), 'unsaved editor stays visible')
    assert.ok(s.instance('OutcomeRow').split(',').includes(identity), 'original editing component remains mounted')
  }
  s.results.useMemoryPages.isError = true
  s.results.useMemoryPages.error = new Error('synthetic refresh failure')
  retained()
  s.results.useMemoryPages.isError = false
  s.results.useMemoryPages.isFetching = true
  retained()
  const newer = { ...original, id: 'synthetic-newer', subject: 'synthetic-other-session' }
  const sameSession = { ...original, id: 'synthetic-same-session' }
  s.results.useMemoryPages.isFetching = false
  for (const facts of [[newer, original], [sameSession, original], [sameSession, newer, original], [sameSession, original]]) {
    s.results.useMemoryPages.data = { ...s.results.useMemoryPages.data, facts }
    retained()
  }
})

test('关注点草稿在后台刷新失败后保留', () => {
  const s = setup('Focus')
  s.find(n => n.type === 'TagInput').props.onChange(['synthetic unsaved'])
  s.results.useFocus.isError = true; s.results.useFocus.error = new Error('synthetic failure')
  assert.equal(s.find(n => n.type === 'TagInput')?.props.tags[0], 'synthetic unsaved')
})

test('来源读取失败显示错误和当前查询重试，不冒充来源缺失', () => {
  const s = setup('Facts')
  s.find(n => n.type === 'button' && n.props['aria-label'] === '查看来源').props.onClick()
  s.results.useSourceEpisodes = { isError: true, error: new Error('synthetic source failure') }
  const error = s.find(n => n.type === 'ErrorState' && n.props.message.includes('synthetic source failure'))
  assert.ok(error)
  error.props.onRetry()
  assert.deepEqual(s.retries, ['useSourceEpisodes'])
})

test('查看会话使用内部路由而非普通链接或硬编码部署前缀', () => {
  const s = setup('Settings')
  const link = s.find(n => n.type === 'Link' && n.props.to === '/conversations')
  assert.ok(link)
  assert.notEqual(link.props.reloadDocument, true)
  assert.equal(s.find(n => n.type === 'a' && n.props.href?.includes('/ui/conversations')), undefined)
})

test('局部错误可被辅助技术识别，重新加载调用当前区域处理器', () => {
  const source = esbuild.buildSync({ entryPoints: [path.resolve(__dirname, '../src/components/ui.tsx')],
    bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
    external: ['react', 'react/jsx-runtime', 'lucide-react', '../store/queryErrors'],
  }).outputFiles[0].text
  const mod = { exports: {} }
  vm.runInNewContext(source, { module: mod, exports: mod.exports, require(name) {
    if (name === '../store/queryErrors') return { useQueryErrors: selector => selector({ failures: [{ message: 'synthetic other failure' }] }) }
    return require(name)
  } })
  let retries = 0
  const result = mod.exports.ErrorState({ message: 'synthetic local failure', onRetry: () => retries++ })
  assert.ok(result)
  assert.equal(mod.exports.ErrorBanner(result.props).props.role, 'alert')
  assert.equal(result.props.action.props.children, '重新加载')
  result.props.action.props.onClick()
  assert.equal(retries, 1)
})

for (const [page, query] of [['Focus', 'useFocus'], ['Health', 'useAudit']]) {
  test(`${page} 首次读取失败提供当前查询重试，不展示空结果`, () => {
    const s = setup(page)
    s.results[query] = { isError: true, error: new Error('synthetic first failure') }
    const error = s.find(n => n.type === 'ErrorState')
    assert.ok(error)
    error.props.onRetry()
    assert.deepEqual(s.retries, [query])
    assert.equal(s.find(n => n.type === 'PageEmptyState'), undefined)
  })
}

test('设置保存失败保留草稿，后续服务器响应不能覆盖 dirty 输入', () => {
  const s = setup('Settings'), input = () => s.find(n => n.type === 'textarea' && n.props.placeholder)
  input().props.onChange({ target: { value: 'synthetic rejected write' } })
  s.find(n => n.type === 'Button' && n.props.loading !== undefined).props.onClick()
  const mutation = s.mutations.find(m => m.name === 'useSetPolicy')
  mutation.args[1].onError(new Error('synthetic write failure'))
  s.results.usePolicy.data = { ...s.results.usePolicy.data, policy: { extract_instruction: 'synthetic server value' } }
  assert.equal(input().props.value, 'synthetic rejected write')
})

test('来源加载、成功空结果和成功原文分别呈现', () => {
  const s = setup('Facts')
  s.find(n => n.type === 'button' && n.props['aria-label'] === '查看来源').props.onClick()
  s.results.useSourceEpisodes = { isLoading: true }
  assert.ok(s.find(n => n.type === 'Spinner'))
  s.results.useSourceEpisodes = { data: { episodes: [] } }
  assert.equal(s.find(n => n.type === 'Spinner'), undefined)
  assert.ok(s.find(n => n.type === 'p' && n.props.children === '来源对话已不在库中(可能被删除了)。'))
  s.results.useSourceEpisodes = { data: { episodes: [{ id: 'synthetic-episode', content: 'synthetic provenance original', date: '2026-10-22', session: 'synthetic' }] } }
  assert.ok(s.find(n => n.type === 'p' && n.props.children === 'synthetic provenance original'))
})

test('接入状态失败不把未知会话数显示为零', () => {
  const s = setup('Settings')
  s.results.useAgentStatus = { isError: true, error: new Error('synthetic status failure') }
  assert.ok(s.find(n => n.type === 'ErrorState' && n.props.message.includes('接入状态读取失败')))
  // No numeric zero is rendered for an unavailable status (other cards have no counts).
  assert.equal(s.nodes().some(n => n.type === 'span' && n.props.children === 0), false)
})
