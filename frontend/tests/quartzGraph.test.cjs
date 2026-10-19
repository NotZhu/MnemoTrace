const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')
const compiled = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/vendor/quartz/model.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
}).outputFiles[0].text
const mod = {exports: {}}
vm.runInNewContext(compiled, {module: mod, exports: mod.exports, setTimeout, clearTimeout, setInterval, clearInterval, performance})
const {createQuartzModel, createQuartzSimulation, configureQuartzForces} = mod.exports
const settings = {nodeScale: 1, lineWidth: 0.8, repelForce: 0.5, centerForce: 0.2, linkDistance: 30, linkForce: 0.6, opacityScale: 3, showRelations: false}
const node = id => ({id, name: `合成实体 ${id}`, type: 'entity'})
const edge = (source, target, live = true) => ({source, target, live, predicate: 'related_to', fact_text: '合成事实'})

test('图谱的生产依赖在禁止动态代码生成时仍能初始化渲染检查并同步 uniform', () => {
  // 使用真实图谱入口和 Pixi，避免 GPU 替身掩盖生产 CSP 与依赖的冲突。
  const source = esbuild.buildSync({
    stdin: {
      contents: "export {createQuartzGraph} from './src/vendor/quartz/graph'; export {AbstractRenderer, GlUniformGroupSystem, UniformGroup} from 'pixi.js'",
      resolveDir: path.resolve(__dirname, '..'),
    },
    bundle: true, platform: 'browser', format: 'cjs', minify: true, write: false,
  }).outputFiles[0].text
  const moduleData = {exports: {}}
  const context = vm.createContext({
    module: moduleData, exports: moduleData.exports,
    navigator: {userAgent: ''}, performance, setTimeout, clearTimeout, setInterval, clearInterval,
  }, {codeGeneration: {strings: false, wasm: false}})
  assert.throws(() => vm.runInContext('new Function("return 1")', context), /Code generation/)
  vm.runInContext(source, context)
  const {AbstractRenderer, GlUniformGroupSystem, UniformGroup} = moduleData.exports
  AbstractRenderer.prototype._unsafeEvalCheck.call({})
  const group = new UniformGroup({uAlpha: {value: 0.75, type: 'f32'}})
  const uniforms = {uAlpha: {type: 'f32', size: 1, location: 'alpha', value: 0}}
  const sync = GlUniformGroupSystem.prototype._generateUniformsSync(group, uniforms)
  const calls = []
  sync(uniforms, group.uniforms, {gl: {uniform1f: (...args) => calls.push(args)}}, {textureCount: 0})
  assert.deepEqual(calls, [['alpha', 0.75]])
})

test('D3 只修改图谱副本，重复/历史关系完整保留，弹簧按实体对合并', () => {
  const data = {nodes: ['a','b','c'].map(node), edges: [edge('a','b'), edge('b','a',false), edge('a','a'), edge('a','missing'), edge('b','c')]}
  const before = JSON.stringify(data)
  const model = createQuartzModel(data)
  assert.equal(model.links.length, 4)
  assert.equal(model.forceLinks.length, 2)
  assert.equal(model.links[1].record.live, false)
  assert.equal(model.links[2].source, model.links[2].target)
  assert.notEqual(model.links[0].record, data.edges[0])
  const simulation = createQuartzSimulation(model, settings)
  simulation.tick(300).stop()
  assert.equal(JSON.stringify(data), before)
  for (const n of model.nodes) assert.ok(Number.isFinite(n.x) && Number.isFinite(n.y))
})

test('新数据保留已有实体坐标，新增实体由 D3 初始化，旧的拖动固定状态不泄漏', () => {
  const previous = [{...node('a'), x: 90, y: -20, vx: 1, vy: 2, fx: 90, fy: -20, radius: 4}]
  const model = createQuartzModel({nodes: [node('a'), node('b')], edges: [edge('a','b')]}, previous)
  assert.equal(model.nodes[0].x, 90)
  assert.equal(model.nodes[0].y, -20)
  assert.equal(model.nodes[0].fx, undefined)
  assert.notEqual(model.nodes[0], previous[0])
  const simulation = createQuartzSimulation(model, settings)
  simulation.stop()
  assert.ok(Number.isFinite(model.nodes[1].x) && Number.isFinite(model.nodes[1].y))
  assert.equal(previous[0].x, 90)
})

test('使用 Quartz 的 D3 受力配置，支持参数调整', () => {
  const model = createQuartzModel({nodes: ['a','b'].map(node), edges: [edge('a','b')]})
  const simulation = createQuartzSimulation(model, settings)
  assert.equal(simulation.force('charge').strength()(model.nodes[0]), -50)
  assert.equal(simulation.force('center').strength(), 0.2)
  assert.ok(Math.abs(simulation.force('x').strength()(model.nodes[0]) - 0.02) < 1e-10)
  assert.equal(simulation.force('link').distance()(model.forceLinks[0]), 30)
  assert.equal(simulation.force('link').strength()(model.forceLinks[0]), 0.6)
  assert.equal(simulation.force('collide').iterations(), 3)
  configureQuartzForces(simulation, model, {...settings, repelForce: 1.2, centerForce: 0.4, linkDistance: 70, linkForce: 0.3, nodeScale: 1.5})
  assert.equal(simulation.force('charge').strength()(model.nodes[0]), -120)
  assert.equal(simulation.force('link').distance()(model.forceLinks[0]), 70)
  assert.equal(simulation.force('link').strength()(model.forceLinks[0]), 0.3)
  assert.equal(simulation.force('radial'), undefined)
  assert.ok(Math.abs(simulation.force('y').strength()(model.nodes[0]) - 0.04) < 1e-10)
  assert.equal(simulation.force('collide').radius()(model.nodes[0]), model.nodes[0].radius * 1.5)
  simulation.stop()
})

test('空图、孤立点、自环、缺失端点及 120 节点均能运行，相同输入结果确定', () => {
  const cases = [
    {nodes: [], edges: []},
    {nodes: [node('single')], edges: [edge('single','single'), edge('single','missing')]},
    {nodes: Array.from({length: 120}, (_, i) => node(String(i))), edges: Array.from({length: 119}, (_, i) => edge('0',String(i+1)))},
  ]
  for (const data of cases) {
    function run() {
      const model = createQuartzModel(data)
      const simulation = createQuartzSimulation(model, settings)
      simulation.tick(300).stop()
      for (const n of model.nodes) assert.ok(Number.isFinite(n.x) && Number.isFinite(n.y))
      return JSON.stringify(model.nodes.map(n => [n.x,n.y]))
    }
    assert.equal(run(), run())
  }
})

test('节点大小反映不同邻居数，自环和重复事实不夸大节点', () => {
  const data = {nodes: ['a','b','c'].map(node), edges: [edge('a','b'),edge('a','c')]}
  const model = createQuartzModel(data)
  const repeated = createQuartzModel({...data, edges: [...data.edges, edge('b','a'), edge('a','a')]})
  assert.ok(model.nodes[0].radius > model.nodes[1].radius)
  assert.deepEqual(model.nodes.map(n => n.radius), repeated.nodes.map(n => n.radius))
})

const React = require('react')
const wrapperSource = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/components/ForceGraph.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../vendor/quartz/graph', '../store/preferences'],
  supported: {'dynamic-import': false},
}).outputFiles[0].text

function wrapperHarness() {
  const states = [], effects = [], calls = []
  let stateIndex = 0, effectIndex = 0, pending = [], created = 0, resolveGraph, observer
  const creation = new Promise(resolve => {resolveGraph = resolve})
  const preferenceState = {graph:{...settings},activeScope:'synthetic-wrapper',setGraph(patch) {this.graph={...this.graph,...patch}},resetGraph() {this.graph={...settings}}}
  const usePreferences = selector => selector(preferenceState)
  usePreferences.getState = () => preferenceState
  const graph = Object.fromEntries(['resize','setData','setLiveOnly','setSelected','configure','destroy','setFocus','zoomBy','panBy','fit','reset'].map(name =>
    [name, (...args) => calls.push({name,args})]))
  const hooks = {
    ...React,
    useId: () => 'synthetic-id',
    useRef: initial => hooks.useState({current: initial})[0],
    useState(initial) {
      const i = stateIndex++
      if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial
      return [states[i], next => {states[i] = typeof next === 'function' ? next(states[i]) : next}]
    },
    useEffect(callback, deps) {
      const i = effectIndex++, previous = effects[i]
      if (!previous || deps.some((value,j) => !Object.is(value, previous.deps[j]))) pending.push(() => {
        previous?.cleanup?.(); effects[i] = {deps, cleanup: callback()}
      })
    },
  }
  const moduleData = {exports: {}}
  vm.runInNewContext(wrapperSource, {
    module: moduleData, exports: moduleData.exports, AbortController,
    ResizeObserver: class {constructor(callback) {observer = callback} observe() {} disconnect() {calls.push({name:'disconnect'})}},
    require(name) {
      if (name === 'react') return hooks
      if (name === '../store/preferences') return {usePreferences,flushPreferences(){}}
      if (name === '../vendor/quartz/graph') return {createQuartzGraph() {created++; return creation}}
      return require(name)
    },
  })
  function findHost(tree) {
    if (!tree || typeof tree !== 'object') return
    if (tree.props?.className === 'graph-canvas') return tree
    for (const child of [].concat(tree.props?.children ?? [])) {const found = findHost(child); if (found) return found}
  }
  function render(props = {data: {nodes: [node('a')], edges: []}, selectedId: null}) {
    stateIndex = effectIndex = 0; pending = []
    const tree = moduleData.exports.ForceGraph(props)
    findHost(tree).ref.current = {clientWidth: 900, clientHeight: 500}
    pending.forEach(callback => callback())
    return tree
  }
  return {render, calls, graph,
    created: () => created,
    async flush() {for (let i=0;i<8;i++) await Promise.resolve()},
    resolve: () => resolveGraph(graph),
    resize: () => observer([{contentRect:{width:700,height:400}}]),
    unmount: () => effects.forEach(effect => effect.cleanup?.()),
  }
}

test('选择、显示设置和窗口尺寸变化复用同一画布，卸载时清理', async () => {
  const h = wrapperHarness(), data = {nodes:[node('a')],edges:[]}
  h.render({data,selectedId:null}); await h.flush()
  assert.equal(h.created(),1)
  h.resolve(); await h.flush()
  h.render({data,selectedId:'a'}); await h.flush()
  assert.equal(h.created(),1)
  assert.equal(h.calls.filter(c=>c.name==='setSelected').at(-1).args[0],'a')
  h.resize()
  assert.equal(h.calls.filter(c=>c.name==='resize').at(-1).args.join(','),'700,400')
  h.unmount()
  assert.equal(h.calls.filter(c=>c.name==='destroy').length,1)
  assert.equal(h.calls.filter(c=>c.name==='disconnect').length,1)
})

test('异步 WebGL 初始化完成前离开页面，迟到的画布会被销毁', async () => {
  const h = wrapperHarness()
  h.render(); await h.flush()
  h.unmount(); h.resolve(); await h.flush()
  assert.equal(h.calls.filter(c=>c.name==='destroy').length,1)
  assert.equal(h.calls.filter(c=>c.name==='resize').length,0)
})

test('仅当前切换复用同一控制器，异步初始化期间变化使用最新显隐值',async()=>{
  const h=wrapperHarness(),data={nodes:[node('a'),node('b')],edges:[edge('a','b',false)]}
  h.render({data,liveOnly:false});await h.flush()
  h.render({data,liveOnly:true});await h.flush()
  h.resolve();await h.flush()
  assert.equal(h.created(),1)
  assert.equal(h.calls.filter(c=>c.name==='setLiveOnly').at(-1).args[0],true)
  const dataCalls=h.calls.filter(c=>c.name==='setData').length
  h.render({data,liveOnly:false});await h.flush()
  assert.equal(h.created(),1)
  assert.equal(h.calls.filter(c=>c.name==='setLiveOnly').at(-1).args[0],false)
  assert.equal(h.calls.filter(c=>c.name==='setData').length,dataCalls,'开关不传入另一份布局数据')
  h.unmount()
})
