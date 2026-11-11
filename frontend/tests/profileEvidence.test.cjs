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
const { ProfileTag } = moduleData.exports
const render = (count, text = '咖啡') => renderToStaticMarkup(
  React.createElement(ProfileTag, { entry: { text, fact_ids: ['a'], fact_count: count } }),
)

test('单条关联事实隐藏次数，多个事实显示乘号和明确含义', () => {
  const single = render(1)
  assert.doesNotMatch(single, /×1|关联 1 条事实/)
  const multiple = render(2)
  assert.match(multiple, /×2/)
  assert.match(multiple, /关联 2 条事实/)
  assert.doesNotMatch(multiple, /button|data-reinforcement|data-manual/)
})

test('画像文字经过 React 转义', () => {
  const html = render(1, '<img src=x onerror=alert(1)>')
  assert.doesNotMatch(html, /<img/)
  assert.match(html, /&lt;img/)
})
