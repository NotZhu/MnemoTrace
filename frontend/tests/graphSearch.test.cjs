const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/GraphPage.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/ui', '../components/ForceGraph', '../hooks/queries', '../store/preferences'],
}).outputFiles[0].text

function findNode(node, predicate) {
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of [].concat(node.props?.children ?? [])) {
    const result = findNode(child, predicate)
    if (result) return result
  }
}

function setup() {
  const states = [], effects = [], timers = new Map(), queries = []
  let stateIndex, effectIndex, pendingEffects, now = 0, timerId = 0
  let result = {data: {nodes: [], edges: []}, isLoading: false, isError: false}
  const hookReact = {
    ...React,
    useRef(initial) {return hookReact.useState({current: initial})[0]},
    useState(initial) {
      const index = stateIndex++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value}]
    },
    useEffect(callback, deps) {
      const index = effectIndex++
      const previous = effects[index]
      if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
        pendingEffects.push(() => {
          previous?.cleanup?.()
          effects[index] = {deps, cleanup: callback()}
        })
      }
    },
  }
  const moduleData = {exports: {}}
  vm.runInNewContext(source, {
    module: moduleData, exports: moduleData.exports,
    window: {
      setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id},
      clearTimeout(id) {timers.delete(id)},
    },
    require(name) {
      if (name === 'react') return hookReact
      if (name === '../store/preferences') return {useSpacePreference(_page,field) {
        return hookReact.useState(field === 'liveOnly')
      }}
      if (name === '../hooks/queries') return {useGraph(params) {queries.push(params); return typeof result === 'function' ? result(params) : result}}
      if (name === '../components/ui') return Object.fromEntries(
        ['Card', 'CardTitle', 'EmptyState', 'ErrorState', 'PageHeader', 'Spinner'].map(key => [key, () => null]),
      )
      if (name === '../components/ForceGraph') return {ForceGraph: () => null}
      return require(name)
    },
  })
  function render() {
    stateIndex = effectIndex = 0
    pendingEffects = []
    const tree = moduleData.exports.default()
    pendingEffects.forEach(callback => callback())
    return tree
  }
  function findInput(node) {
    if (!node || typeof node !== 'object') return undefined
    if (node.type === 'input') return node
    for (const child of [].concat(node.props?.children ?? [])) {
      const input = findInput(child)
      if (input) return input
    }
  }
  return {
    render, queries,
    graph: () => findNode(render(), node => typeof node.props?.onSelect === 'function'),
    inspector: () => findNode(render(), node => node.type === 'aside'),
    closeDetails: () => findNode(render(), node => node.type === 'button' && node.props['aria-label'] === '关闭节点详情'),
    toggleCurrent: () => findNode(render(), node => node.props?.role === 'switch' && node.props['aria-label'] === '仅当前').props.onClick(),
    input: () => findInput(render()),
    setResult(value) {result = value},
    tick(ms) {
      now += ms
      for (const [id, timer] of timers) {
        if (timer.at <= now) {timers.delete(id); timer.callback()}
      }
      render()
    },
    query: () => queries.at(-1).q,
  }
}

test('图谱连续输入后等待 500ms 才提交最终搜索词', () => {
  const s = setup()
  s.input().props.onChange({target: {value: 'Bei'}})
  s.render()
  assert.equal(s.query(), '')
  s.tick(300)
  s.input().props.onChange({target: {value: 'Beijing'}})
  s.render()
  s.tick(499)
  assert.equal(s.query(), '')
  assert.equal(s.input().props.value, 'Beijing')
  s.tick(1)
  assert.equal(s.query(), 'Beijing')
})

test('中文组词期间不提交拼音，完成后延迟搜索，清空立即恢复', () => {
  const s = setup()
  s.input().props.onCompositionStart()
  s.render()
  s.input().props.onChange({target: {value: 'bei'}})
  s.render()
  s.tick(1000)
  assert.equal(s.query(), '')
  s.input().props.onChange({target: {value: '北京'}})
  s.input().props.onCompositionEnd()
  s.render()
  s.tick(500)
  assert.equal(s.query(), '北京')
  s.input().props.onChange({target: {value: ''}})
  s.render()
  s.render()
  assert.equal(s.query(), '')
})

test('加载和出错时搜索框仍可继续输入', () => {
  const s = setup()
  s.input().props.onChange({target: {value: 'Beijing'}})
  s.render()
  for (const result of [
    {isLoading: true, isError: false},
    {isLoading: false, isError: true, error: new Error('synthetic failure')},
  ]) {
    s.setResult(result)
    assert.ok(s.input(), 'search field must remain mounted')
    assert.equal(s.input().props.value, 'Beijing')
  }
})

const syntheticGraph = {
  nodes: [{id: 'synthetic-project', name: '演示项目', type: 'project'}, {id: 'synthetic-tool', name: '演示工具', type: 'entity'}],
  edges: [{source: 'synthetic-project', target: 'synthetic-tool', predicate: 'uses', live: true, fact_id: 'synthetic-fact', fact_text: '演示项目使用演示工具。', valid_at_h: '2026-10-15'}],
}

test('选中关系任一端都保持原始方向和英文关系词，仅加粗当前实体并去除重复正文', () => {
  const s = setup()
  const data = {
    nodes: [{id:'friend',name:'演示朋友',type:'entity'},{id:'fruit',name:'演示水果',type:'entity'}],
    edges: [{source:'friend',target:'fruit',predicate:'likes',live:true,fact_text:'演示朋友 likes 演示水果'}],
  }
  const original = JSON.stringify(data)
  function text(node) {
    if (node == null || typeof node === 'boolean') return ''
    if (typeof node !== 'object') return String(node)
    return [].concat(node.props?.children ?? []).map(text).join('')
  }
  s.setResult({data,isLoading:false,isError:false})
  for (const [id, name] of [['fruit','演示水果'],['friend','演示朋友']]) {
    s.graph().props.onSelect(id)
    const row = findNode(s.inspector(), node => node.type === 'li')
    const title = [].concat(row.props.children)[0]
    assert.equal(text(title), '演示朋友 likes 演示水果')
    assert.equal(text(findNode(title,node=>node.type==='strong')), name)
    assert.equal(text(row).split('演示朋友 likes 演示水果').length - 1, 1)
  }
  assert.equal(JSON.stringify(data), original)
})

test('实体卡片始终保留，选择才显示详情，清除选择后恢复空内容', () => {
  const s = setup()
  s.setResult({data: syntheticGraph, isLoading: false, isError: false})
  assert.ok(s.inspector())
  assert.equal(findNode(s.inspector(), node => node.props?.children === '演示项目'), undefined)
  s.graph().props.onSelect('synthetic-project')
  assert.ok(findNode(s.inspector(), node => node.props?.children === '演示项目'))
  assert.ok(findNode(s.inspector(), node => node.props?.children === '演示项目使用演示工具。'))
  const canvas = findNode(s.render(), node => node.type === 'section' && node.props.className === 'graph-workspace')
  assert.equal(findNode(canvas, node => node.type === 'aside'), undefined, 'details must not overlay the canvas')
  s.closeDetails().props.onClick()
  assert.ok(s.inspector())
  assert.equal(findNode(s.inspector(), node => node.props?.children === '演示项目'), undefined)
  assert.equal(s.graph().props.selectedId, null)
  assert.equal(s.graph().props.data, syntheticGraph)
})

test('筛选移除实体或图谱为空时清空详情，加载和失败仍保留实体卡片', () => {
  const s = setup()
  s.setResult({data: syntheticGraph, isLoading: false, isError: false})
  s.graph().props.onSelect('synthetic-project')
  s.setResult({data: {nodes: [syntheticGraph.nodes[1]], edges: []}, isLoading: false, isError: false})
  assert.equal(s.graph().props.selectedId, null, 'missing selection must not dim all remaining nodes')
  for (const result of [
    {data: {nodes: [], edges: []}, isLoading: false, isError: false},
    {isLoading: true, isError: false},
    {isLoading: false, isError: true, error: new Error('synthetic failure')},
  ]) {
    s.setResult(result)
    assert.ok(s.inspector())
    assert.equal(findNode(s.inspector(), node => node.props?.children === '演示项目'), undefined)
  }
})

test('仅当前同步查询条件、可见统计与详情，过滤后不恢复隐藏选择', () => {
  const s = setup()
  const data = {nodes:[...syntheticGraph.nodes,{id:'old',name:'合成历史实体',type:'entity'}],
    edges:[...syntheticGraph.edges,{source:'synthetic-project',target:'old',predicate:'used_to',live:false}]}
  s.setResult({data,isLoading:false,isError:false})
  assert.equal(s.graph().props.data,data,'布局始终接收完整关系')
  assert.equal(s.queries.at(-1).live_only,true,'服务端在限量前筛选当前关系')
  assert.equal(s.graph().props.liveOnly,true)
  assert.ok(findNode(s.render(),n=>n.type==='h3' && n.props.children==='2 个实体 · 1 条关系'))
  s.graph().props.onSelect('synthetic-project')
  assert.equal(countRows(s.inspector()),1)
  s.toggleCurrent()
  assert.equal(s.graph().props.data,data)
  assert.equal(s.graph().props.liveOnly,false)
  assert.ok(findNode(s.render(),n=>n.type==='h3' && n.props.children==='3 个实体 · 2 条关系'))
  assert.equal(countRows(s.inspector()),2)
  s.graph().props.onSelect('old')
  s.toggleCurrent()
  assert.equal(s.graph().props.selectedId,null)
  s.toggleCurrent()
  assert.equal(s.graph().props.selectedId,null,'已隐藏的选择不应再次出现')
  assert.equal(s.queries.at(-1).live_only,false)
  function countRows(tree) {const list=findNode(tree,n=>n.type==='ul');return [].concat(list?.props.children??[]).length}
})

test('120 条历史不能让命名空间中的当前关系显示为空', () => {
  const s = setup()
  const history = Array.from({length:121}, (_,i) => ({source:'old-source',target:'old-target',predicate:`old ${i}`,live:false}))
  s.setResult(params => {
    const edges = [...history, ...syntheticGraph.edges].filter(edge => !params.live_only || edge.live)
    return {data:{nodes:[...syntheticGraph.nodes,{id:'old-source'},{id:'old-target'}],edges:edges.slice(0,params.limit),has_more:edges.length>params.limit},isLoading:false,isError:false}
  })
  const tree = s.render()
  assert.equal(s.graph().props.data.edges[0].fact_id,'synthetic-fact')
  assert.equal(findNode(tree,n=>n.props?.className==='graph-empty-state'),undefined)
})

test('截断提示仅描述当前成功结果，过渡和错误不显示确定性空态', () => {
  const s = setup()
  s.setResult({data:{...syntheticGraph,has_more:true},isLoading:false,isError:false})
  assert.ok(findNode(s.render(),n=>n.props?.children==='已展示 1 条关系，还有更多，请缩小搜索范围。'))
  for (const result of [
    {data:{nodes:[],edges:[],has_more:true},isPlaceholderData:true,isFetching:true},
    {isLoading:true},
    {isError:true,error:new Error('synthetic failure')},
  ]) {
    s.setResult(result)
    assert.ok(s.graph(),'请求期间保留画布')
    const tree=s.render()
    assert.equal(findNode(tree,n=>n.props?.className==='graph-empty-state'),undefined)
    assert.equal(findNode(tree,n=>n.props?.children==='已展示 1 条关系，还有更多，请缩小搜索范围。'),undefined)
  }
})

test('截断旧图的过渡空结果不清除最终当前图中仍存在的选择',()=>{
  const s=setup()
  s.toggleCurrent()
  const broad={nodes:[syntheticGraph.nodes[0],{id:'old',name:'合成历史目标',type:'entity'}],
    edges:[{source:'synthetic-project',target:'old',predicate:'used',live:false}],has_more:true}
  s.setResult({data:broad,isLoading:false,isError:false})
  s.graph().props.onSelect('synthetic-project')
  s.setResult({data:broad,isPlaceholderData:true,isFetching:true})
  s.toggleCurrent()
  s.render()
  s.setResult({data:{...syntheticGraph,has_more:false},isLoading:false,isError:false})
  assert.equal(s.graph().props.selectedId,'synthetic-project')
})

test('仅有历史关系时显示空态但保留完整画布，开关恢复内容', () => {
  const s=setup(),data={...syntheticGraph,edges:syntheticGraph.edges.map(e=>({...e,live:false}))}
  s.setResult({data,isLoading:false,isError:false})
  assert.ok(s.graph(),'过滤为空时仍保留画布实例')
  assert.equal(s.graph().props.data,data)
  assert.equal(s.graph().props.liveOnly,true)
  assert.ok(findNode(s.render(),n=>n.type==='h3' && n.props.children==='0 个实体 · 0 条关系'))
  s.toggleCurrent()
  assert.equal(s.graph().props.data,data)
  assert.ok(findNode(s.render(),n=>n.type==='h3' && n.props.children==='2 个实体 · 1 条关系'))
})
