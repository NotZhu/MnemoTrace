const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Health.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../hooks/queries'],
}).outputFiles[0].text

function find(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of [].concat(node.props?.children ?? [])) {
    const result = find(child, predicate)
    if (result) return result
  }
}

function render(report, status = {}) {
  const moduleData = { exports: {} }
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    require(name) {
      if (name === 'react') return {
        ...React, useState: initial => [initial, () => {}],
        useRef: initial => ({ current: initial }), useEffect() {},
      }
      if (name === '../hooks/queries') return { useAudit: () => ({ data: report, ...status }) }
      if (name === '../components/ui') return Object.fromEntries(
        ['Button', 'Card', 'CardTitle', 'ErrorState', 'PageHeader', 'Spinner'].map(key => [key, () => null]),
      )
      return require(name)
    },
  })
  return moduleData.exports.default()
}

test('图谱体检展示服务端顺序和记录 ID，且没有事实编辑或删除操作', () => {
  const findings = [
    { kind: 'missing_edge', fact_id: 'fact-a' },
    { kind: 'edge_mismatch', fact_id: 'fact-b', edge_id: 'edge-b' },
    { kind: 'orphan_entity', entity_id: 'entity-c' },
  ]
  const page = render({
    checked: { facts: 2, entities: 3 },
    total_findings: 4,
    by_kind: { missing_edge: 2, edge_mismatch: 1, orphan_entity: 1 },
    findings,
    truncated: true,
  })
  for (const finding of findings) {
    const row = find(page, node => node.props?.finding === finding)
    assert.ok(row)
    const detail = row.type(row.props)
    for (const id of [finding.fact_id, finding.edge_id, finding.entity_id].filter(Boolean)) {
      assert.ok(find(detail, node => node.type === 'code' && node.props.children === id))
    }
    assert.equal(find(detail, node => node.type === 'button'), undefined)
  }
  assert.ok(find(page, node => node.props?.children === '当前只显示部分检查结果。'))
})

test('图谱体检在刷新失败时保留已有结果', () => {
  const page = render({
    checked: { facts: 0, entities: 0 }, total_findings: 0, by_kind: {}, findings: [],
  }, { isError: true, error: new Error('synthetic') })
  assert.ok(find(page, node => node.props?.message?.includes('显示上次结果')))
  assert.ok(find(page, node => node.props?.title === '暂未发现图谱异常'))
})
