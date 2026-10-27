const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

function load(entry) {
  const source = esbuild.buildSync({ entryPoints: [path.resolve(__dirname, entry)], bundle: true,
    platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
    external: ['react', 'react/jsx-runtime'] }).outputFiles[0].text
  const mod = { exports: {} }
  vm.runInNewContext(source, { module: mod, exports: mod.exports, require })
  return mod.exports
}
const { topicPatch, TOPIC_OPTIONS } = load('../src/lib/topics.ts')
const { TopicBadge, TopicEditor } = load('../src/components/TopicEditor.tsx')

test('未操作主题不发送字段，显式选择当前值和未知仍是人工设置，重置不夹带主题', () => {
  assert.equal(JSON.stringify(topicPatch('keep')), '{}')
  assert.equal(JSON.stringify(topicPatch('food')), '{"topic":"food"}')
  assert.equal(JSON.stringify(topicPatch('unknown')), '{"topic":"unknown"}')
  assert.equal(JSON.stringify(topicPatch('reset')), '{"reset_topic":true}')
})

test('主题标签枚举完整，未知保留为可选值', () => {
  assert.equal(TOPIC_OPTIONS.length, 15)
  assert.equal(new Set(TOPIC_OPTIONS.map(o => o.value)).size, 15)
  assert.equal(TOPIC_OPTIONS.find(o => o.value === 'unknown').label, '未知')
})

test('主题和主题依据展示独立于事实来源', () => {
  for (const [source, label] of [['rule', '规则判断'], ['llm', '模型判断'], ['user', '人工设置'], ['unset', '未确定']]) {
    const html = renderToStaticMarkup(React.createElement(TopicBadge, { topic: 'unknown', topic_source: source }))
    assert.match(html, /未知/)
    assert.ok(html.includes(label))
  }
})

test('编辑默认保持主题，解释正文变化会重判；新建默认自动判断且不能重置', () => {
  const edit = renderToStaticMarkup(React.createElement(TopicEditor, {
    current: { topic: 'food', topic_source: 'rule' }, value: 'keep', onChange() {}, disabled: true,
  }))
  assert.match(edit, /不修改主题/)
  assert.match(edit, /正文/)
  assert.match(edit, /重新判断/)
  assert.match(edit, /disabled/)
  const add = renderToStaticMarkup(React.createElement(TopicEditor, { value: 'keep', onChange() {} }))
  assert.match(add, /自动判断/)
  assert.doesNotMatch(add, /value="reset"/)
})
