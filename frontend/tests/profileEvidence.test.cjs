const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Profile.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/Toast', '../hooks/queries'],
}).outputFiles[0].text
const moduleData = { exports: {} }
vm.runInNewContext(source, {
  module: moduleData, exports: moduleData.exports,
  require: name => {
    if (name === '../components/Toast' || name === '../hooks/queries') return {}
    return require(name)
  },
})
const { EvidenceChip } = moduleData.exports
const render = evidence => renderToStaticMarkup(React.createElement(EvidenceChip, { e: evidence }))
const visibleText = html => html.replace(/<[^>]+>/g, '')

test('自动提取事实显示来源数', () => {
  const html = render({ kind: 'mentions', count: 2, source_count: 2 })
  assert.equal(visibleText(html), 'x2')
  assert.doesNotMatch(html, /手动设定|已增强/)
})

test('用户设定事实只显示来源数，不显示手动设定标记', () => {
  const html = render({ kind: 'user', count: 1, source_count: 2 })
  assert.equal(visibleText(html), 'x2')
  assert.doesNotMatch(html, /lucide-user-check|手动设定|经你添加、修改或确认/)
})

test('访问强化次数不会当作来源数显示', () => {
  const html = render({ kind: 'reinforced', count: 7, source_count: 1 })
  assert.match(html, /x1/)
  assert.match(html, /已增强/)
  assert.match(html, /title="曾被系统检索使用，提高了重要程度。"/)
  assert.doesNotMatch(visibleText(html), /x7/)
})

test('没有来源的用户设定事实不显示证据标记', () => {
  const html = render({ kind: 'user', count: 1, source_count: 0 })
  assert.equal(html, '')
})

test('无直接来源的身份补全不显示虚构计数', () => {
  assert.equal(render({ kind: 'mentions', count: 1, source_count: 0 }), '')
})
