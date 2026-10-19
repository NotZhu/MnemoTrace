const assert = require('node:assert/strict')
const test = require('node:test')
const vm = require('node:vm')
const path = require('node:path')
const esbuild = require('esbuild')

function load(entry) {
  const bundled = esbuild.buildSync({
    entryPoints: [path.resolve(__dirname, '../src/pages', entry)],
    bundle: true, platform: 'node', format: 'cjs', write: false,
  }).outputFiles[0].text
  const moduleData = { exports: {} }
  vm.runInNewContext(bundled, { module: moduleData, exports: moduleData.exports })
  return moduleData.exports
}

const graph = load('loginMemoryGraphData.ts')
const { edgeColors } = load('loginMemoryGraphAppearance.ts')
const appearance = load('loginMemoryGraphAppearance.ts')
const motionApi = load('loginMemoryGraphMotion.ts')
const at = (x, y) => graph.nodes.findIndex(node => node.x === x && node.y === y)
const hasEdge = (a, b) => graph.edges.some(edge => edge.from === a && edge.to === b || edge.from === b && edge.to === a)
const key = edge => [edge.from, edge.to].sort((a, b) => a - b).join('-')
const paintKey = edge => [edge.from, edge.to].map(index => `${graph.nodes[index].x},${graph.nodes[index].y}`).sort().join('|')

function contrast(color) {
  const light = hex => {
    const rgb = [1, 3, 5].map(index => {
      const channel = parseInt(hex.slice(index, index + 2), 16) / 255
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    })
    return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722
  }
  return (light(color) + 0.05) / (light('#12171f') + 0.05)
}

test('参与电流传播的底线在静置时也有足够对比度，原有亮线不变', () => {
  graph.edges.forEach(edge => {
    const source = edgeColors[paintKey(edge)]
    const stroke = appearance.edgeStrokes[paintKey(edge)]
    assert(contrast(stroke) >= 3, `${paintKey(edge)}: ${stroke} 对比度 ${contrast(stroke).toFixed(2)}`)
    if (contrast(source) >= 3) assert.equal(stroke, source)
  })
})

test('右上分支经过中间节点，不额外画跨节点捷径', () => {
  const hub = at(927, 377), middle = at(1008, 386), end = at(1074, 379)
  assert(!hasEdge(hub, end), '跨过中间节点的长线造成两个节点之间的双线')
  assert(hasEdge(hub, middle))
  assert(hasEdge(middle, end))
})

test('近乎背景色的连接不能直接传递电流', () => {
  const from = at(1021, 634), to = at(950, 503)
  assert(!hasEdge(from, to))
  assert(!graph.nodes[from].neighbors.includes(to))
  assert.notEqual(graph.distances(from)[to], 1)
})

test('每个电流邻居都有且只有一条绘制连接', () => {
  const unique = new Set(graph.edges.map(key))
  assert.equal(unique.size, graph.edges.length)
  graph.nodes.forEach((node, index) => {
    assert.equal(new Set(node.neighbors).size, node.neighbors.length)
    node.neighbors.forEach(neighbor => {
      assert(unique.has([index, neighbor].sort((a, b) => a - b).join('-')))
      assert(graph.nodes[neighbor].neighbors.includes(index))
    })
  })
  assert.equal(graph.nodes.reduce((sum, node) => sum + node.neighbors.length, 0), graph.edges.length * 2)
  graph.edges.forEach(edge => assert(edgeColors[paintKey(edge)], '连接需要可见的底线颜色'))
})

test('任意起点的一跳传播都与可见连线一致', () => {
  graph.nodes.forEach((_, origin) => {
    const depths = graph.distances(origin)
    depths.forEach((depth, destination) => {
      assert.equal(depth === 1, hasEdge(origin, destination), `${origin} → ${destination}`)
    })
  })
})

test('常驻电流从不同区域的已连接节点轮流出发', () => {
  assert.equal(graph.ambientOrigins.length, 6)
  assert.equal(new Set(graph.ambientOrigins).size, graph.ambientOrigins.length)
  graph.ambientOrigins.forEach(origin => assert(graph.nodes[origin].neighbors.length > 0))
})

test('常驻电流的每个亮点都有对应的可见入边，且在到达之后亮起', () => {
  graph.ambientOrigins.forEach(origin => {
    const signal = graph.createAmbientSignal(origin)
    assert(signal.paths.length > 0)
    assert.equal(signal.litNodes.length, signal.paths.length + 1)
    const arrivals = new Map(signal.litNodes.map(node => [node.index, node.delayMs]))
    const destinations = new Set()
    signal.paths.forEach(path => {
      assert(graph.edges.includes(path.edge))
      assert(hasEdge(path.from, path.to))
      assert(!destinations.has(path.to), '同一节点不重复叠加入边电流')
      destinations.add(path.to)
      assert.equal(path.delayMs, arrivals.get(path.from))
      assert.equal(path.delayMs + path.durationMs, arrivals.get(path.to))
      assert(path.depth >= 0 && path.depth < 3)
    })
    signal.litNodes.forEach(node => {
      assert(node.depth >= 0 && node.depth <= 3)
      assert(node.index === origin || destinations.has(node.index))
    })
    assert(signal.durationMs < graph.AMBIENT_INTERVAL_MS, '前一次尾光结束后再开始下一次电流')
  })
})

test('成组漂浮的邻近节点协调移动，自动幅度保持收敛', () => {
  for (const time of [1, 4, 9, 17, 33]) {
    const homes = motionApi.motionHomes(time)
    homes.forEach((point, index) => assert(Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) < 10))
    const hub = at(700, 253), neighbor = at(634, 283)
    const a = homes[hub], b = homes[neighbor], baseA = graph.nodes[hub], baseB = graph.nodes[neighbor]
    assert(Math.hypot((a.x - baseA.x) - (b.x - baseB.x), (a.y - baseA.y) - (b.y - baseB.y)) < 5)
  }
})

test('弹性拖拽牵动可见邻居、限制范围，松手后回稳', () => {
  const motion = motionApi.createGraphMotion()
  const origin = at(700, 253), home = graph.nodes[origin]
  motion.pin(origin, { x: home.x + 80, y: home.y + 30 }, 0.5)
  for (let frame = 0; frame < 90; frame++) motion.step(16, false)
  assert(Math.hypot(motion.positions[origin].x - home.x, motion.positions[origin].y - home.y) > 60)
  assert(home.neighbors.some(index => Math.hypot(motion.positions[index].x - graph.nodes[index].x, motion.positions[index].y - graph.nodes[index].y) > 10))
  motion.pin(origin, { x: home.x + 10000, y: home.y + 10000 }, 0.5)
  assert(Math.hypot(motion.positions[origin].x - home.x, motion.positions[origin].y - home.y) <= motionApi.GRAPH_MOTION.dragRadius)
  for (let frame = 0; frame < 120; frame++) motion.step(16, false)
  motion.positions.forEach((point, index) => assert(Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) <= 130.001))
  motion.release()
  for (let frame = 0; frame < 900; frame++) motion.step(16, false)
  assert(Math.max(...motion.positions.map((point, index) => Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y))) < 1)
})

test('边和正反向电流路径共用移动后的端点，不增加新的连接', () => {
  const motion = motionApi.createGraphMotion()
  for (let frame = 0; frame < 240; frame++) motion.step(16)
  graph.edges.forEach(edge => {
    const forward = motion.path(edge).match(/-?\d+(?:\.\d+)?/g).map(Number)
    const reverse = motion.path(edge, true).match(/-?\d+(?:\.\d+)?/g).map(Number)
    const a = motion.positions[edge.from], b = motion.positions[edge.to]
    assert(Math.hypot(forward[0] - a.x, forward[1] - a.y) < 0.01)
    assert(Math.hypot(forward[6] - b.x, forward[7] - b.y) < 0.01)
    assert.deepEqual(forward, [reverse[6], reverse[7], reverse[4], reverse[5], reverse[2], reverse[3], reverse[0], reverse[1]])
  })
})

test('脑形边界同时约束自动漂浮和长距离拖拽，后台时长不积压', () => {
  const motion = motionApi.createGraphMotion(), origin = at(700, 253), home = graph.nodes[origin]
  motion.setBoundary((index, point) => Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) <= 12)
  motion.pin(origin, { x: home.x + 1000, y: home.y }, 1)
  for (let frame = 0; frame < 100; frame++) motion.step(16)
  motion.positions.forEach((point, index) => assert(Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) <= 12.001))
  const normal = motionApi.createGraphMotion(), resumed = motionApi.createGraphMotion()
  normal.step(40); resumed.step(10000)
  assert.deepEqual(normal.positions, resumed.positions)
})

test('局部舒展可以独立于漂浮调整，关闭所有自动动态时保持原坐标', () => {
  const amplitude = motionApi.GRAPH_MOTION.floatAmplitude
  motionApi.GRAPH_MOTION.floatAmplitude = 0
  try {
    const homes = motionApi.motionHomes(5)
    assert(homes.some((point, index) => Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) > 0.2))
    assert(homes.every((point, index) => Math.hypot(point.x - graph.nodes[index].x, point.y - graph.nodes[index].y) < 2))
    motionApi.motionHomes(5, false).forEach((point, index) => {
      assert.equal(point.x, graph.nodes[index].x)
      assert.equal(point.y, graph.nodes[index].y)
    })
  } finally { motionApi.GRAPH_MOTION.floatAmplitude = amplitude }
})
