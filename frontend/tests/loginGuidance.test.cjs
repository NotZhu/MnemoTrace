const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Login.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  loader: { '.css': 'empty' },
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/ThemeToggle',
    '../store/auth', '../hooks/queries', './LoginMemoryGraph', './LoginParticles'],
}).outputFiles[0].text

function nodes(tree) {
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...(Array.isArray(tree) ? tree : [].concat(tree.props?.children ?? [])).flatMap(nodes)]
}
function text(tree) {
  if (typeof tree === 'string') return tree
  if (!tree || typeof tree !== 'object') return ''
  return (Array.isArray(tree) ? tree : [].concat(tree.props?.children ?? [])).map(text).join('')
}
function setup(mode = 'open') {
  const states = [], logins = []
  let cursor = 0, sequence = 0
  const health = mode === 'pending' ? { isPending: true } : mode === 'error' ? { isSuccess: false }
    : { isSuccess: true, data: { auth_mode: mode } }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    crypto: { randomUUID: () => `synthetic-${++sequence}` },
    require(name) {
      if (name === 'react') return { ...React, useRef: () => ({ current: null }), useState(initial) {
        const index = cursor++
        if (!(index in states)) states[index] = initial
        return [states[index], value => { states[index] = value }]
      } }
      if (name === '../store/auth') return { useAuth: selector => selector({ login: key => logins.push(key), keyError: null }) }
      if (name === '../hooks/queries') return { useHealth: () => health }
      if (['../components/ui', '../components/ThemeToggle', './LoginMemoryGraph', './LoginParticles'].includes(name)) {
        return new Proxy({}, { get: () => () => null })
      }
      return require(name)
    },
  })
  function render() { cursor = 0; return moduleData.exports.default() }
  const find = predicate => nodes(render()).find(predicate)
  return { logins, render,
    input: () => find(node => node.type === 'input'),
    button: label => find(node => node.props?.type === 'button' && text(node).trim() === label),
    submit: () => find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }),
    clear: () => find(node => node.props?.className === 'login-clear-key').props.onClick(),
  }
}

test('开放模式说明已有空间的返回方式，生成 Key 不显示额外小字也不自动登录', () => {
  const s = setup()
  assert(text(s.render()).includes('开放模式：使用相同 Key，即可回到原来的记忆空间。'))
  assert.equal(s.input().props.placeholder, '输入已有空间的 Key')
  assert.equal(s.button('生成新的空间 Key').props.disabled, false)
  s.button('生成新的空间 Key').props.onClick()
  assert.equal(s.input().props.value, 'mnemotrace-synthetic-1')
  assert(!text(s.render()).includes('请保存此 Key'))
  assert.equal(s.input().props['aria-describedby'], 'login-key-notice')
  assert.deepEqual(s.logins, [])
  s.submit()
  assert.deepEqual(s.logins, ['mnemotrace-synthetic-1'])
})

test('生成后仍可修改、清空或填入演示 Key，演示填入不直接登录', () => {
  const s = setup()
  s.button('生成新的空间 Key').props.onClick()
  s.input().props.onChange({ target: { value: 'synthetic-existing-space' } })
  assert.equal(s.input().props.value, 'synthetic-existing-space')
  s.button('生成新的空间 Key').props.onClick()
  s.clear()
  assert.equal(s.input().props.value, '')
  s.button('生成新的空间 Key').props.onClick()
  s.button('填入演示空间 Key').props.onClick()
  assert.equal(s.input().props.value, 'mnemotrace-test')
  assert.deepEqual(s.logins, [])
})

test('受保护模式使用管理员 Key 说明，两个快捷按钮保持显示并禁用', () => {
  const s = setup('api_keys')
  assert(text(s.render()).includes('受保护模式：请输入管理员提供的 API Key，进入对应的记忆空间。'))
  assert.equal(s.input().props.placeholder, '输入 API Key')
  for (const label of ['生成新的空间 Key', '填入演示空间 Key']) assert.equal(s.button(label).props.disabled, true)
  s.input().props.onChange({ target: { value: 'synthetic-authorized-key' } })
  s.submit()
  assert.deepEqual(s.logins, ['synthetic-authorized-key'])
})

test('模式检测中、不可用和禁用时保留快捷按钮的禁用状态', () => {
  for (const mode of ['pending', 'error', 'disabled']) {
    const s = setup(mode)
    for (const label of ['生成新的空间 Key', '填入演示空间 Key']) assert.equal(s.button(label).props.disabled, true)
  }
})
