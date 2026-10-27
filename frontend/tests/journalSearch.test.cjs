const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Journal.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime', '../hooks/queries', '../store/preferences', '../hooks/useDebouncedSearch'],
}).outputFiles[0].text

function nodes(tree) {
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...(Array.isArray(tree) ? tree : [].concat(tree.props?.children ?? [], tree.props?.footer ?? [])).flatMap(nodes)]
}
function text(node) {
  if (typeof node === 'string') return node
  if (!node || typeof node !== 'object') return ''
  return (Array.isArray(node) ? node : [].concat(node.props?.children ?? [])).map(text).join('')
}
function setup({ empty = false, history = false } = {}) {
  const states = [], queries = [], openQueries = []
  let cursor = 0, fetching = false, debounced, topic = ''
  const fact = { id: 'synthetic-conclusion', subject: 'synthetic-session', predicate: 'decision', object: 'Synthetic decision', text: 'Synthetic decision', status: 'live', valid_at: '2026-10-16 12:00' }
  const mutation = () => ({ mutate() {}, isPending: false })
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    require(name) {
      if (name === 'react') return {
        ...React, useEffect() {}, useRef: () => ({ current: null }),
        useState(initial) {
          const index = cursor++
          if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
          return [states[index], next => { states[index] = typeof next === 'function' ? next(states[index]) : next }]
        },
      }
      if (name === '../store/preferences') return { useSpacePreference: (_, field) => field === 'topic'
        ? [topic, next => { topic = next }] : [history, next => { history = typeof next === 'function' ? next(history) : next }] }
      if (name === '../hooks/useDebouncedSearch') return { useDebouncedSearch: q => debounced === undefined ? q : debounced }
      if (name === '../hooks/queries') return {
        useMemories: params => { openQueries.push(params); return { data: { facts: [] } } },
        useMemoryPages(params) {
          queries.push(params)
          const facts = empty || params.q.trim() || (params.outcome_kind && params.outcome_kind !== 'decision') ? [] : [fact]
          return { data: { facts, facts_page: { has_more: false } }, isLoading: false, isFetching: fetching, isError: false }
        },
        useSessions: () => ({ data: { sessions: [] } }),
        useCloseSession: mutation, useEditFact: mutation, useDeleteFact: mutation,
      }
      return require(name)
    },
  })
  function render() { cursor = 0; return moduleData.exports.default() }
  const all = () => nodes(render())
  return {
    render, queries, openQueries,
    topic(value) { all().find(node => node.type?.name === 'FilterSelect' && node.props.label === '主题').props.onChange(value) },
    search(value) { all().find(node => node.type === 'input').props.onChange({ target: { value } }) },
    kind(label) { all().find(node => node.props?.label === label).props.onClick() },
    click(label) { all().find(node => node.type === 'button' && text(node).trim() === label).props.onClick() },
    button(label) { return all().find(node => node.type === 'button' && text(node).trim() === label) },
    title(value) { return all().some(node => node.props?.title === value) },
    empty() { return all().some(node => node.type?.name === 'JournalEmpty') },
    loading() { return all().some(node => node.props?.label === '正在搜索结论…') },
    pending(value, query) { fetching = value; debounced = query },
  }
}

test('真正空列表保留首条总结入口，有搜索条件时显示没有匹配的结论', () => {
  const s = setup({ empty: true })
  assert.ok(s.empty())
  assert.ok(!s.title('没有匹配的结论'))
  s.search('不存在的话题')
  assert.ok(s.title('没有匹配的结论'))
  assert.ok(!s.empty())
  assert.ok(s.button('重置筛选'))
})

test('重置筛选位于空状态底部，恢复已有结论并保留历史设置', () => {
  const s = setup({ history: true })
  s.search('不存在的话题')
  const tree = nodes(s.render())
  const container = tree.find(node => node.type === 'div' && [].concat(node.props.children ?? []).some(child => child?.props?.role === 'switch'))
  assert.equal(nodes(container).filter(node => node.type === 'button').length, 1)
  const empty = tree.find(node => node.props?.title === '没有匹配的结论')
  assert.equal(empty.props.footer.props['aria-label'], '重置筛选')
  assert.equal(tree.filter(node => node.type === 'button' && text(node).trim() === '重置筛选').length, 1)
  s.click('重置筛选')
  assert.ok(!s.title('没有匹配的结论'))
  assert.ok(!s.button('重置筛选'))
  assert.equal(s.queries.at(-1).q, '')
  assert.equal(s.queries.at(-1).status, undefined)
})

test('只有类型筛选时可以重置，搜索和类型同时存在时一次清除', () => {
  const s = setup()
  s.kind('发现')
  assert.ok(s.title('没有匹配的结论'))
  assert.ok(s.button('重置筛选'))
  s.search('不存在的话题')
  assert.ok(s.button('重置筛选'))
  s.click('重置筛选')
  assert.ok(!s.title('没有匹配的结论'))
  assert.ok(!s.button('重置筛选'))
  assert.ok(!s.empty())
})

test('等待防抖或查询完成时不提前展示空结果', () => {
  const s = setup({ empty: true })
  s.search('不存在的话题')
  s.pending(false, '')
  assert.ok(s.loading())
  assert.ok(!s.title('没有匹配的结论'))
  assert.ok(!s.empty())
  s.pending(true, '不存在的话题')
  assert.ok(s.loading())
  s.pending(false, '不存在的话题')
  assert.ok(s.title('没有匹配的结论'))
})


test('类型筛选作为服务端查询条件发送', () => {
  const s = setup()
  s.kind('经验')
  s.render()
  assert.equal(s.queries.at(-1).outcome_kind, 'lesson')
})

test('主题筛选随请求发送且清空筛选恢复全部主题', () => {
  const s = setup()
  s.topic('unknown')
  s.render()
  assert.equal(s.queries.at(-1).topic, 'unknown')
  assert.equal(s.openQueries.at(-1).topic, 'unknown')
  s.click('重置筛选')
  s.render()
  assert.equal(s.queries.at(-1).topic, undefined)
  assert.equal(s.openQueries.at(-1).topic, undefined)
})
