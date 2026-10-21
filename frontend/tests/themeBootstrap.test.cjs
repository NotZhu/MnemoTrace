const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const root = path.resolve(__dirname, '..')

test('主题在同步同源脚本中恢复，HTML 不含内联脚本', () => {
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
  assert.match(html, /<script src="\/theme-init.js"><\/script>/)
  assert.ok(!/<script\s*>/.test(html))
  const source = fs.readFileSync(path.join(root, 'public/theme-init.js'), 'utf8')
  for (const [saved, unavailable, expected] of [
    [null, false, 'dark'], ['{"state":{"theme":"light"}}', false, 'light'],
    ['broken', false, 'dark'], ['{"state":{"theme":"invalid"}}', false, 'dark'],
    [null, true, 'dark'],
  ]) {
    const document = { documentElement: { dataset: {}, style: {} } }
    const localStorage = { getItem() { if (unavailable) throw Error('synthetic'); return saved } }
    vm.runInNewContext(source, { document, localStorage })
    assert.equal(document.documentElement.dataset.theme, expected)
    assert.equal(document.documentElement.style.colorScheme, expected)
  }
})
