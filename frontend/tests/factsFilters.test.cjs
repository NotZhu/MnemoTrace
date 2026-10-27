const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Facts.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', '../components/ui', '../components/Modal', '../components/Toast', '../hooks/queries', '../store/preferences'],
}).outputFiles[0].text
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...(Array.isArray(tree) ? tree : [].concat(tree.props?.children ?? [], tree.props?.footer ?? [], tree.props?.action ?? [])).flatMap(nodes)]
}
function setup({ empty = false, hideSensitive = false } = {}) {
  const states = [], effects = [], queries = [], preferences = { status: 'live', category: '', topic: '', hideSensitive }
  let queryResult = {}, retries = 0
  let cursor, effectCursor, pending
  const mutation = () => ({ mutate() {} })
  const moduleData = { exports: {} }
  vm.runInNewContext(source, { module: moduleData, exports: moduleData.exports, require(name) {
    if (name === 'react') return { ...React, useRef: () => ({ current: null }),
      useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }] },
      useEffect(callback, deps) { const index = effectCursor++; if (!effects[index] || deps.some((value, i) => !Object.is(value, effects[index][i]))) { effects[index] = deps; pending.push(callback) } },
    }
    if (name === '../store/preferences') return { useSpacePreference: (_, field) => [preferences[field], value => { preferences[field] = typeof value === 'function' ? value(preferences[field]) : value }] }
    if (name === '../hooks/queries') return { useAddFact: mutation, useEditFact: mutation, useDeleteFact: mutation, useSourceEpisodes: () => ({}),
      useMemoryPages(params) { queries.push(params); return { isSuccess: true, loadMore: () => {}, refetch: () => { retries++ }, data: { facts: empty ? [] : [{ id: 'synthetic-fact', display: 'Synthetic fact', status: 'live', valid_at: '', provenance: [] }], fact_categories: ['工作', '其他', '历史分类'], facts_page: { total: empty ? 0 : 81, has_more: !empty } }, ...queryResult } },
    }
    if (name === '../components/ui') return new Proxy({}, { get: () => () => null })
    if (name === '../components/Modal') return { Modal: () => null, ConfirmDialog: () => null }
    if (name === '../components/Toast') return { toast: {} }
    return require(name)
  } })
  function render() { cursor = effectCursor = 0; pending = []; const tree = moduleData.exports.default(); pending.forEach(effect => effect()); return tree }
  const find = predicate => nodes(render()).find(predicate)
  return { render, preferences, queries,
    setResult: result => { queryResult = result }, retries: () => retries,
    select: label => find(node => node.type?.name === 'FilterSelect' && node.props.label === label),
    search: value => find(node => node.type === 'input').props.onChange({ target: { value } }),
    button: label => find(node => node.props?.children === label || (node.type === 'button' && node.props['aria-label'] === label)),
    query() { render(); return queries.at(-1) },
  }
}

test('分类、主题和状态下拉框位于隐藏敏感开关左侧', () => {
  const s = setup(), controls = nodes(s.render()).filter(node => node.type?.name === 'FilterSelect' || node.props?.role === 'switch')
  assert.deepEqual(controls.map(node => node.props.label ?? node.props['aria-label']), ['事实分类', '主题', '事实状态', '隐藏敏感内容'])
  assert.equal(s.select('事实分类').props.value, '')
  assert.equal(s.select('事实状态').props.value, 'live')
  assert.deepEqual(Array.from(s.select('事实分类').props.options, option => option.value), ['', '工作', '其他', '历史分类'])
})

test('加载时保留开关和筛选，旧计数、分类、列表及分页一起退出', () => {
  const s = setup()
  s.select('事实分类').props.onChange('工作')
  s.setResult({ data: undefined, isLoading: true, isFetching: true, isSuccess: false })
  s.button('隐藏敏感内容').props.onClick()
  assert.equal(s.query().include_sensitive, false)
  assert.deepEqual(Array.from(s.select('事实分类').props.options, o => o.value), ['', '工作'])
  const tree = nodes(s.render())
  assert.ok(tree.some(n => n.props?.label === '加载事实…'))
  assert.ok(tree.some(n => n.props?.children === '正在统计…'))
  assert.equal(s.button('加载更多事实'), undefined)
  assert.equal(tree.some(n => n.type?.name === 'PageEmptyState'), false)
})

test('失败时工具栏和当前查询重试仍可用，不误报空结果', () => {
  const s = setup()
  s.setResult({ data: undefined, isLoading: false, isError: true, isSuccess: false, error: new Error('synthetic failure') })
  assert.ok(s.button('隐藏敏感内容'))
  assert.ok(nodes(s.render()).some(n => n.props?.message === 'synthetic failure'))
  nodes(s.render()).find(n => n.props?.message === 'synthetic failure').props.onRetry()
  assert.equal(s.retries(), 1)
  assert.equal(nodes(s.render()).some(n => n.type?.name === 'PageEmptyState'), false)
})

test('占位列表标明更新并禁用加载更多，旧计数不作为当前统计', () => {
  const s = setup()
  s.setResult({ isPlaceholderData: true, isFetching: true })
  assert.equal(s.button('加载更多事实').props.disabled, true)
  assert.ok(nodes(s.render()).some(n => n.props?.children === '正在更新事实…'))
  assert.ok(nodes(s.render()).some(n => n.props?.children === '正在统计…'))
})

test('旧空列表不能被解释成新搜索无结果，成功响应后才显示空状态', () => {
  const s = setup({ empty: true })
  s.setResult({ isPlaceholderData: true, isFetching: true })
  assert.equal(nodes(s.render()).some(n => n.type?.name === 'PageEmptyState'), false)
  s.setResult({ isPlaceholderData: false, isFetching: false })
  assert.equal(nodes(s.render()).some(n => n.type?.name === 'PageEmptyState'), true)
})

test('问答与隐私保留全局提示，接入页由自己的状态错误占据顶部', () => {
  const layout = esbuild.buildSync({
    entryPoints: [path.resolve(__dirname, '../src/components/Layout.tsx')],
    bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
    external: ['react', 'react/jsx-runtime', 'react-router-dom', './GlobalErrorBanner', './Sidebar', './Topbar', './Toast', '../store/queryErrors'],
  }).outputFiles[0].text
  for (const route of ['/facts', '/profile', '/settings', '/conversations', '/focus', '/journal', '/health', '/ask', '/graph', '/timeline', '/', '/privacy']) {
    const moduleData = { exports: {} }, Outlet = () => null, GlobalErrorBanner = () => null
    vm.runInNewContext(layout, { module: moduleData, exports: moduleData.exports, require(name) {
      if (name === 'react') return { ...React, useState: () => [false, () => {}] }
      if (name === 'react-router-dom') return { Outlet, useMatch: pattern => route === pattern }
      if (name === './GlobalErrorBanner') return { GlobalErrorBanner }
      if (name === '../store/queryErrors') return { useQueryErrors: selector => selector({ failures: [{ message: 'synthetic failure' }] }) }
      if (name.startsWith('./')) return new Proxy({}, { get: () => () => null })
      return require(name)
    } })
    assert.equal(nodes(moduleData.exports.default()).some(n => n.type === Outlet), true, route)
    assert.equal(nodes(moduleData.exports.default()).some(n => n.type === GlobalErrorBanner), ['/ask', '/privacy'].includes(route), route)
  }
})

test('分类、历史状态和搜索词共同送入服务查询，切换筛选后重置分页', () => {
  const s = setup()
  s.button('加载更多事实').props.onClick()
  assert.equal(s.query().facts_limit, 40)
  s.select('事实分类').props.onChange('工作')
  s.select('主题').props.onChange('technology')
  s.select('事实状态').props.onChange('superseded')
  s.search('synthetic')
  s.render()
  const query = s.query()
  assert.equal(query.facts_limit, 40)
  assert.equal(query.kind, 'attributes')
  assert.equal(query.category, '工作')
  assert.equal(query.topic, 'technology')
  assert.equal(query.status, 'superseded')
  assert.equal(query.q, 'synthetic')
  assert.equal(query.include_sensitive, true)
  s.select('事实状态').props.onChange('all')
  assert.equal(s.query().status, undefined)
})

test('无结果时通过底部重置筛选清空分类、状态和搜索，隐藏敏感设置保持不变', () => {
  const s = setup({ empty: true, hideSensitive: true })
  s.select('事实分类').props.onChange('工作')
  s.select('事实状态').props.onChange('all')
  s.select('主题').props.onChange('unknown')
  s.search('不存在的合成内容')
  const empty = nodes(s.render()).find(node => node.type?.name === 'PageEmptyState')
  assert.equal(empty.props.footer.props['aria-label'], '重置筛选')
  s.button('重置筛选').props.onClick()
  const query = s.query()
  assert.equal(query.category, undefined)
  assert.equal(query.topic, undefined)
  assert.equal(query.status, 'live')
  assert.equal(query.q, '')
  assert.equal(query.include_sensitive, false)
})
