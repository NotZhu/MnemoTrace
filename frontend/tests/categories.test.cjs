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
const { CATEGORY_OPTIONS, savedCategories } = load('../src/lib/categories.ts')
const { CATEGORIES } = load('../src/generated/memory-contract.ts')
const { CategoryBadges } = load('../src/components/CategoryEditor.tsx')

test('category catalog contains 15 distinct labels and validates saved filters', () => {
  assert.equal(CATEGORY_OPTIONS.length, 15)
  assert.equal(new Set(CATEGORY_OPTIONS.map(([value]) => value)).size, 15)
  assert.deepEqual(Array.from(CATEGORY_OPTIONS, ([value]) => value), Array.from(CATEGORIES))
  assert.deepEqual(Array.from(savedCategories(['food', 'Music', 'music', 'food'])), ['food', 'music'])
  assert.equal(savedCategories('music').length, 0)
})

test('category badges show every label and its source', () => {
  const html = renderToStaticMarkup(React.createElement(CategoryBadges, {
    categories: ['food', 'music'], categories_source: 'llm',
  }))
  assert.match(html, /饮食/)
  assert.match(html, /音乐/)
  assert.match(html, /模型判断/)
})
