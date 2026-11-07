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
function setup(state = 'ready') {
  const states = [], logins = []
  let cursor = 0
  const health = state === 'pending' ? { isPending: true } : state === 'error' ? { isError: true }
    : { data: { ready: state === 'ready', auth_mode: 'api_keys' } }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
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
    submit: () => find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }),
    clear: () => find(node => node.props?.className === 'login-clear-key').props.onClick(),
  }
}

test('服务就绪时提示输入已签发的 API Key', () => {
  const s = setup()
  assert(text(s.render()).includes('受保护模式：请输入管理员提供的 API Key，进入对应的记忆空间。'))
  assert.equal(s.input().props.placeholder, '输入 API Key')
  s.submit()
  assert.deepEqual(s.logins, [])
  s.input().props.onChange({ target: { value: 'synthetic-authorized-key' } })
  s.submit()
  assert.deepEqual(s.logins, ['synthetic-authorized-key'])
  s.clear()
  assert.equal(s.input().props.value, '')
})

test('区分服务检查中与不可用', () => {
  assert(text(setup('pending').render()).includes('正在检查服务状态…'))
  for (const state of ['error', 'unready']) {
    const s = setup(state)
    assert(text(s.render()).includes('服务暂未就绪，请检查服务连接。'))
    assert.equal(s.input().props.placeholder, '输入 API Key')
  }
})
