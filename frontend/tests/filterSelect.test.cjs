const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({ stdin: {
  contents: "export {FilterSelect} from './FilterSelect'; export {ScopeSelect} from './ScopeSelect'; export {ExportRangeSelect} from './ExportRangeSelect'",
  resolveDir: path.resolve(__dirname, '../src/components'), loader: 'tsx',
},
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic', external: ['react', 'react/jsx-runtime'],
}).outputFiles[0].text
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return []
  return [tree, ...(Array.isArray(tree) ? tree : [].concat(tree.props?.children ?? [])).flatMap(nodes)]
}
function setup(component = 'FilterSelect', initialValue = 'live') {
  const states = [], changes = [], effects = [], listeners = new Map()
  let cursor = 0, effectCursor = 0, pending = [], value = initialValue, disabled = false
  const moduleData = { exports: {} }
  vm.runInNewContext(source, { module: moduleData, exports: moduleData.exports,
    document: { addEventListener(name, fn) { listeners.set(name, fn) }, removeEventListener(name) { listeners.delete(name) } },
    require(name) {
    if (name === 'react') return { ...React, useId: () => 'synthetic-filter', useRef: () => ({ current: null }),
      useEffect(callback, deps) {
        const index = effectCursor++, previous = effects[index]
        if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) pending.push(() => {
          previous?.cleanup?.()
          effects[index] = { deps, cleanup: callback() }
        })
      },
      useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], next => { states[index] = next }] },
    }
    return require(name)
  } })
  function render() { cursor = 0; effectCursor = 0
    const props = { label: '事实状态', value, disabled,
    options: [{ value: 'live', label: '当前有效' }, { value: 'superseded', label: '历史事实' }, { value: 'all', label: '全部状态' }],
    onChange(next) { value = next; changes.push(next) },
    }
    const element = moduleData.exports[component](props)
    const tree = component === 'FilterSelect' ? element : element.type(element.props)
    const callbacks = pending; pending = []; callbacks.forEach(callback => callback())
    return tree
  }
  const find = predicate => nodes(render()).find(predicate)
  return { render, changes,
    disable(next) { disabled = next; render() },
    outside() { listeners.get('pointerdown')?.({ target: {} }) },
    trigger: () => find(node => node.props?.role === 'combobox'),
    list: () => find(node => node.props?.role === 'listbox'),
    option: label => find(node => node.props?.role === 'option' && node.props.children[0].props.children === label),
    key(key) { this.trigger().props.onKeyDown({ key, preventDefault() {}, stopPropagation() {} }) },
  }
}

test('触发器无边框无下划线，展开菜单使用主题底色、高亮和勾选标记', () => {
  const s = setup(), trigger = s.trigger()
  assert(trigger.props.className.includes('border-0'))
  assert(trigger.props.className.includes('bg-transparent'))
  assert(trigger.props.className.includes('focus-visible:outline-none'))
  assert(!trigger.props.className.includes('underline'))
  assert.equal(s.list(), undefined)
  trigger.props.onClick()
  assert(s.list().props.className.includes('bg-ink-800'))
  assert(s.option('当前有效').props.className.includes('bg-selected'))
  assert.equal(s.option('当前有效').props['aria-selected'], true)
  assert.equal(s.trigger().props['aria-controls'], s.list().props.id)
})

test('鼠标与键盘选择更新筛选，取消、失焦和悬停不意外切换', () => {
  const s = setup()
  s.trigger().props.onClick()
  s.option('历史事实').props.onPointerMove()
  assert.deepEqual(s.changes, [])
  s.key('Enter')
  assert.deepEqual(s.changes, ['superseded'])
  assert.equal(s.list(), undefined)
  s.key('ArrowDown'); s.key('End'); s.key('Enter')
  assert.deepEqual(s.changes, ['superseded', 'all'])
  s.trigger().props.onClick()
  s.option('当前有效').props.onClick()
  assert.deepEqual(s.changes, ['superseded', 'all', 'live'])
  s.key('ArrowDown'); s.key('Escape')
  assert.equal(s.list(), undefined)
  s.trigger().props.onClick()
  s.render().props.onBlur({ currentTarget: { contains: () => false }, relatedTarget: null })
  assert.equal(s.list(), undefined)
  assert.equal(s.changes.length, 3)
})

test('记忆范围复用选择行为并保留选项值、标签关联和样式', () => {
  const s = setup('ScopeSelect', 'auto'), trigger = s.trigger()
  const label = nodes(s.render()).find(node => node.props?.id === trigger.props['aria-labelledby'])
  assert.equal(label.props.children, '保存方式')
  assert(trigger.props.className.includes('min-h-8'))
  s.key('ArrowDown'); s.key('End'); s.key('Enter')
  assert.deepEqual(s.changes, ['working'])
  s.key('ArrowDown'); s.key('Home'); s.key('Enter')
  assert.deepEqual(s.changes, ['working', 'auto'])
  s.trigger().props.onClick()
  assert.equal(s.list().props['aria-labelledby'], s.trigger().props['aria-labelledby'])
  assert(s.list().props.className.includes('w-32'))
  s.outside()
  assert.equal(s.list(), undefined)
})

test('导出范围保留说明关联，禁用时关闭菜单并阻止选择，再启用可正常使用', () => {
  const s = setup('ExportRangeSelect', 'compact')
  assert.equal(s.trigger().props['aria-describedby'], 'privacy-export-mode-hint')
  s.trigger().props.onClick()
  assert(s.list().props.className.includes('w-48'))
  s.disable(true)
  assert.equal(s.trigger().props.disabled, true)
  assert.equal(s.trigger().props['aria-expanded'], false)
  assert.equal(s.list(), undefined)
  s.trigger().props.onClick(); s.key('End'); s.key('Enter')
  assert.deepEqual(s.changes, [])
  s.disable(false)
  assert.equal(s.list(), undefined)
  s.key('ArrowDown'); s.key('End'); s.key('Enter')
  assert.deepEqual(s.changes, ['with-originals'])
  s.trigger().props.onClick(); s.key('Tab')
  assert.equal(s.list(), undefined)
})
