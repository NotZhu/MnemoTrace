const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Health.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/Modal', '../components/Toast', '../hooks/queries'],
}).outputFiles[0].text

function find(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of [].concat(node.props?.children ?? [])) {
    const result = find(child, predicate)
    if (result) return result
  }
}

function setup(report, overrides = {}) {
  const calls = [], deleteCalls = [], states = []
  let index = 0
  const hookReact = {
    ...React,
    useState(initial) {
      const slot = index++
      if (!(slot in states)) states[slot] = initial
      return [states[slot], value => { states[slot] = typeof value === 'function' ? value(states[slot]) : value }]
    },
    useRef(initial) { return hookReact.useState({ current: initial })[0] },
    useEffect() {},
    useLayoutEffect() {},
  }
  const finding = { kind: 'machine_token', fact_id: 'synthetic_fact', subject: 'user', predicate: 'occupation', object: 'ROLE_TOKEN', text: 'user occupation ROLE_TOKEN', display: '演示用户的职业是 ROLE_TOKEN', ...overrides }
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    require(name) {
      if (name === 'react') return hookReact
      if (name === '../hooks/queries') return {
        useAudit: () => ({ data: report ?? { checked: { facts: 1, entities: 1 }, total_findings: 1, by_kind: { machine_token: 1 }, findings: [finding] } }),
        useClearSlot: () => ({ isPending: false }),
        useDeleteFact: () => ({ isPending: false, mutate: (id, callbacks) => deleteCalls.push({ id, callbacks }) }),
        useEditFact: () => ({ isPending: false, mutate: (request, callbacks) => calls.push({ request, callbacks }) }),
      }
      if (name === '../components/ui') return Object.fromEntries(['Button', 'Card', 'CardTitle', 'EmptyState', 'ErrorState', 'PageHeader', 'Spinner'].map(key => [key, () => null]))
      if (name === '../components/Modal') return { ConfirmDialog: () => null }
      if (name === '../components/Toast') return { toast: { success() {}, error() {} } }
      return require(name)
    },
  })
  const page = moduleData.exports.default()
  const row = find(page, node => node.props?.finding === finding)
  states.length = 0
  function render() { index = 0; return row.type(row.props) }
  function button(label) { return find(render(), node => node.type === 'button' && node.props['aria-label'] === label) }
  function editor() { return find(render(), node => ['StatementEditor', 'AttributeEditor'].includes(node.type?.name)) }
  function dialog() { return find(render(), node => node.props?.title === '永久删除这条记忆？') }
  return { calls, deleteCalls, page, render, editor, button, dialog, finding, start() { button('编辑这条记忆').props.onClick() } }
}

test('属性编辑提交明确三元组，不能把完整展示句作为 object 或 statement 保存', () => {
  const s = setup()
  s.start()
  assert.equal(s.editor().type.name, 'AttributeEditor')
  assert.deepEqual(JSON.parse(JSON.stringify(s.editor().props.value)), {
    subject: 'user', predicate: 'occupation', object: 'ROLE_TOKEN',
  })
  s.editor().props.onChange({ subject: 'synthetic_user', predicate: 'job_title', object: '产品设计师' })
  s.button('保存').props.onClick()
  s.button('保存').props.onClick()
  assert.equal(s.calls.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0].request.patch)), {
    subject: 'synthetic_user', predicate: 'job_title', object: '产品设计师',
  })
  s.calls[0].callbacks.onSuccess({ display: '演示用户是产品设计师。', text: 'synthetic_user job title 产品设计师' })
  assert.equal(s.editor(), undefined)
  assert.ok(find(s.render(), node => node.props?.children === '演示用户是产品设计师。'))
})

test('属性编辑的原样保存、取消及空字段不提交，失败保留三元组草稿', () => {
  const s = setup()
  s.start()
  s.button('保存').props.onClick()
  assert.equal(s.calls.length, 0)
  assert.equal(s.editor(), undefined)
  s.start()
  s.editor().props.onChange({ subject: 'user', predicate: 'occupation', object: '  ' })
  assert.equal(s.button('保存').props.disabled, true)
  s.button('保存').props.onClick()
  assert.equal(s.calls.length, 0)
  s.button('取消').props.onClick()
  s.start()
  s.editor().props.onChange({ subject: 'user', predicate: 'occupation', object: '产品设计师' })
  s.button('保存').props.onClick()
  s.calls[0].callbacks.onError(new Error('synthetic error'))
  s.calls[0].callbacks.onSettled()
  assert.equal(s.editor().props.value.object, '产品设计师')
  s.button('保存').props.onClick()
  assert.equal(s.calls.length, 2)
})

test('属性表单分别绑定主语、关系和取值，字段失焦不自动提交', () => {
  const s = setup()
  s.start()
  assert.equal(s.editor().type(s.editor().props).props.children.length, 3)
  for (const [index, field] of ['subject', 'predicate', 'object'].entries()) {
    const element = s.editor()
    const fields = element.type(element.props).props.children
    const input = find(fields[index], node => node.type === 'input')
    assert.equal(input.props.value, s.finding[field])
    assert.equal(input.props.onBlur, undefined)
    input.props.onChange({ target: { value: `changed-${field}` } })
    assert.equal(s.editor().props.value[field], `changed-${field}`)
  }
  assert.deepEqual(JSON.parse(JSON.stringify(s.editor().props.value)), {
    subject: 'changed-subject', predicate: 'changed-predicate', object: 'changed-object',
  })
  assert.equal(s.calls.length, 0)
})

test('结论仍可整句编辑，重复失焦不重复保存', () => {
  const s = setup(undefined, { predicate: 'lesson' })
  s.start()
  assert.equal(s.editor().props.value, s.finding.display)
  s.editor().props.onChange('演示用户是产品设计师。')
  s.editor().props.onBlur({ relatedTarget: null })
  s.editor().props.onBlur({ relatedTarget: null })
  assert.equal(s.calls.length, 1)
  assert.equal(s.calls[0].request.patch.statement, '演示用户是产品设计师。')
  assert.equal(s.calls[0].request.patch.object, undefined)
})

test('取消和原样失焦不提交，保存失败保留草稿并允许重试', () => {
  const s = setup(undefined, { predicate: 'lesson' })
  s.start()
  s.editor().props.onChange('取消的草稿')
  s.button('取消').props.onClick()
  assert.equal(s.editor(), undefined)
  assert.equal(s.calls.length, 0)
  s.start()
  s.editor().props.onBlur({ relatedTarget: null })
  assert.equal(s.calls.length, 0)
  s.start()
  s.editor().props.onChange('保留的草稿')
  s.button('保存').props.onClick()
  s.calls[0].callbacks.onError(new Error('synthetic error'))
  s.calls[0].callbacks.onSettled()
  assert.equal(s.editor().props.value, '保留的草稿')
  s.button('保存').props.onClick()
  assert.equal(s.calls.length, 2)
})

test('按钮接管失焦，取消不会先保存', () => {
  const s = setup(undefined, { predicate: 'lesson' })
  s.start()
  s.editor().props.onChange('不应提交的草稿')
  const marker = {}
  const actions = find(s.render(), node => node.ref != null)
  actions.ref.current = { contains: target => target === marker }
  s.editor().props.onBlur({ relatedTarget: marker })
  s.button('取消').props.onClick()
  assert.equal(s.calls.length, 0)
})

test('单条删除先打开项目弹窗，取消不提交，确认才删除且不重复请求', () => {
  const s = setup()
  assert.equal(s.dialog().props.open, false)
  s.button('删除').props.onClick()
  assert.equal(s.dialog().props.open, true)
  assert.equal(s.deleteCalls.length, 0)
  assert.ok(find(s.dialog().props.body, node => node.props?.children === s.finding.display))
  s.dialog().props.onClose()
  assert.equal(s.dialog().props.open, false)
  assert.equal(s.deleteCalls.length, 0)
  s.button('删除').props.onClick()
  s.dialog().props.onConfirm()
  s.dialog().props.onConfirm()
  assert.equal(s.deleteCalls.length, 1)
  assert.equal(s.deleteCalls[0].id, s.finding.fact_id)
  s.deleteCalls[0].callbacks.onSuccess()
  s.deleteCalls[0].callbacks.onSettled()
  assert.equal(s.dialog().props.open, false)
})

test('删除失败保留确认弹窗并可重试', () => {
  const s = setup()
  s.button('删除').props.onClick()
  s.dialog().props.onConfirm()
  s.deleteCalls[0].callbacks.onError(new Error('synthetic error'))
  s.deleteCalls[0].callbacks.onSettled()
  assert.equal(s.dialog().props.open, true)
  s.dialog().props.onConfirm()
  assert.equal(s.deleteCalls.length, 2)
})

test('汇总标签与问题列表均按颜色优先级排序，同级稳定且不改写查询缓存', () => {
  const original = ['orphan_entity', 'code_artifact', 'machine_token', 'empty_value', 'fragment', 'unreduced_claim', 'embedder_blind', 'slot_overflow', 'dangling_subject']
  const findings = Object.freeze(original.map((kind, i) => Object.freeze({ kind, fact_id: `synthetic-${i}`, text: kind })))
  const by_kind = Object.freeze(Object.fromEntries(original.map(kind => [kind, 1])))
  const s = setup({ checked: { facts: 9, entities: 9 }, total_findings: 9, by_kind, findings })
  const labels = [], rows = []
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (node.type?.name === 'FindingLabel') labels.push(node.props.kind)
    if (node.props?.finding) rows.push(node.props.finding.kind)
    for (const child of [].concat(node.props?.children ?? [])) visit(child)
  }
  visit(s.page)
  const expected = ['empty_value', 'embedder_blind', 'machine_token', 'fragment', 'slot_overflow', 'dangling_subject', 'code_artifact', 'unreduced_claim', 'orphan_entity']
  assert.deepEqual(labels, expected)
  assert.deepEqual(rows, expected)
  assert.deepEqual(findings.map(finding => finding.kind), original)
  assert.deepEqual(Object.keys(by_kind), original)
})
