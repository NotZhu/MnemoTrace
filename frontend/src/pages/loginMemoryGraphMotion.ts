import { nodes, edges, type BrainEdge } from './loginMemoryGraphData'

export type Position = { x: number; y: number }
type Particle = Position & { vx: number; vy: number }

// 三个效果独立调节；自动幅度按页面反馈再次收敛，单位为 SVG 坐标。
export const GRAPH_MOTION = {
  floatAmplitude: 0.22,
  floatSpeed: 1,
  breathAmplitude: 1.4,
  breathSpeed: 0.84,
  curveBreath: 0.9,
  spring: 10,
  neighborPull: 4,
  homePull: 2.4,
  damping: 3.4,
  dragRadius: 130,
  dragScreenRadius: 64,
}

const groups = [
  { x: 355, y: 265, phase: 0 }, { x: 715, y: 260, phase: 0.8 },
  { x: 1030, y: 485, phase: 1.55 }, { x: 990, y: 745, phase: 2.35 },
  { x: 500, y: 740, phase: 3.1 }, { x: 835, y: 945, phase: 3.9 },
]
const weights = nodes.map(node => {
  const values = groups.map(group => Math.exp(-((node.x - group.x) ** 2 + (node.y - group.y) ** 2) / 235 ** 2))
  const total = values.reduce((sum, value) => sum + value, 0)
  return values.map(value => value / total)
})
const geometry = new Map(edges.map(edge => {
  const values = edge.path.match(/-?\d+(?:\.\d+)?/g)!.map(Number)
  return [`${edge.from}-${edge.to}`, { c1: { x: values[2], y: values[3] }, c2: { x: values[4], y: values[5] } }]
}))
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value))

export function motionHomes(time: number, moving = true): Position[] {
  const fade = 1 - Math.exp(-time / 0.95)
  return nodes.map((node, index) => {
    if (!moving) return { x: node.x, y: node.y }
    let dx = 0, dy = 0
    groups.forEach((group, region) => {
      const weight = weights[index][region]
      const phase = group.phase
      const breath = Math.sin(time * GRAPH_MOTION.breathSpeed + phase) * GRAPH_MOTION.breathAmplitude
      dx += weight * (Math.sin(time * 0.94 * GRAPH_MOTION.floatSpeed + phase) * 24 * GRAPH_MOTION.floatAmplitude + (node.x - group.x) / 250 * breath)
      dy += weight * (Math.cos(time * 0.85 * GRAPH_MOTION.floatSpeed + phase * 0.9) * 19 * GRAPH_MOTION.floatAmplitude + (node.y - group.y) / 250 * breath)
    })
    return {
      x: node.x + fade * (dx + Math.sin(time * 0.63 + node.y / 420) * 4 * GRAPH_MOTION.floatAmplitude),
      y: node.y + fade * (dy + Math.sin(time * 0.7 + node.x / 530) * 3 * GRAPH_MOTION.floatAmplitude),
    }
  })
}

export function createGraphMotion() {
  const positions: Particle[] = nodes.map(node => ({ x: node.x, y: node.y, vx: 0, vy: 0 }))
  let time = 0
  let previousHomes = motionHomes(0)
  let pin: { index: number; point: Position } | null = null
  // 注入脑形包含检查，物理计算本身不依赖 DOM，测试可使用合成边界。
  let contains: ((index: number, point: Position) => boolean) | null = null

  function confine(index: number, point: Position, radius = GRAPH_MOTION.dragRadius, soft = false): Position {
    const home = nodes[index], dx = point.x - home.x, dy = point.y - home.y
    const length = Math.hypot(dx, dy)
    const distance = soft && length > radius * 0.62
      ? radius * (0.62 + 0.38 * (1 - Math.exp(-(length / radius - 0.62) / 0.38)))
      : Math.min(length, radius)
    let bounded = length ? { x: home.x + dx * distance / length, y: home.y + dy * distance / length } : { x: home.x, y: home.y }
    if (contains && !contains(index, bounded)) {
      let low = 0, high = 1
      for (let turn = 0; turn < 10; turn++) {
        const fraction = (low + high) / 2
        const trial = { x: home.x + (bounded.x - home.x) * fraction, y: home.y + (bounded.y - home.y) * fraction }
        if (contains(index, trial)) low = fraction
        else high = fraction
      }
      bounded = { x: home.x + (bounded.x - home.x) * low, y: home.y + (bounded.y - home.y) * low }
    }
    return bounded
  }

  function step(milliseconds: number, moving = true) {
    // 分帧积分并丢弃后台积压，切回页面或低帧率时不会瞬间飞出。
    const dt = clamp(milliseconds, 0, 40) / 1000
    if (!dt) return
    if (moving) time += dt
    const homes = motionHomes(time, moving)
    const velocities = homes.map((home, index) => ({
      x: clamp((home.x - previousHomes[index].x) / dt, -55, 55),
      y: clamp((home.y - previousHomes[index].y) / dt, -55, 55),
    }))
    previousHomes = homes
    const steps = Math.ceil(dt / 0.00834), h = dt / steps
    for (let turn = 0; turn < steps; turn++) {
      if (pin) Object.assign(positions[pin.index], pin.point, { vx: 0, vy: 0 })
      const forces = positions.map((point, index) => ({
        x: (homes[index].x - point.x) * GRAPH_MOTION.homePull + velocities[index].x * GRAPH_MOTION.damping,
        y: (homes[index].y - point.y) * GRAPH_MOTION.homePull + velocities[index].y * GRAPH_MOTION.damping,
      }))
      for (const edge of edges) {
        const a = positions[edge.from], b = positions[edge.to]
        const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1
        const homeX = homes[edge.to].x - homes[edge.from].x, homeY = homes[edge.to].y - homes[edge.from].y
        const strength = 1 / Math.sqrt(Math.max(1, (nodes[edge.from].neighbors.length + nodes[edge.to].neighbors.length) / 2))
        const stretch = (length - Math.hypot(homeX, homeY)) * GRAPH_MOTION.spring * strength
        const fx = dx / length * stretch + (dx - homeX) * GRAPH_MOTION.neighborPull * strength
        const fy = dy / length * stretch + (dy - homeY) * GRAPH_MOTION.neighborPull * strength
        forces[edge.from].x += fx; forces[edge.from].y += fy
        forces[edge.to].x -= fx; forces[edge.to].y -= fy
      }
      positions.forEach((point, index) => {
        if (pin?.index === index) return
        const damp = Math.exp(-GRAPH_MOTION.damping * h), mass = 1 + nodes[index].radius * 0.015
        point.vx = clamp((point.vx + forces[index].x * h / mass) * damp, -420, 420)
        point.vy = clamp((point.vy + forces[index].y * h / mass) * damp, -420, 420)
        point.x += point.vx * h; point.y += point.vy * h
        const bounded = confine(index, point)
        if (Math.hypot(bounded.x - point.x, bounded.y - point.y) > 0.05) { point.vx *= 0.15; point.vy *= 0.15 }
        Object.assign(point, bounded)
      })
    }
  }

  function path(edge: BrainEdge, reverse = false, moving = true) {
    const a = positions[edge.from], b = positions[edge.to]
    const baseA = nodes[edge.from], baseB = nodes[edge.to], shape = geometry.get(`${edge.from}-${edge.to}`)!
    const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1
    const breath = moving ? (1 - Math.exp(-time / 0.95)) * Math.sin(time * 0.95 + (baseA.x + baseB.x) / 600 + (baseA.y + baseB.y) / 850) * GRAPH_MOTION.curveBreath : 0
    const c1 = { x: shape.c1.x + (a.x - baseA.x) * 2 / 3 + (b.x - baseB.x) / 3 - dy / length * breath, y: shape.c1.y + (a.y - baseA.y) * 2 / 3 + (b.y - baseB.y) / 3 + dx / length * breath }
    const c2 = { x: shape.c2.x + (a.x - baseA.x) / 3 + (b.x - baseB.x) * 2 / 3 - dy / length * breath, y: shape.c2.y + (a.y - baseA.y) / 3 + (b.y - baseB.y) * 2 / 3 + dx / length * breath }
    const p = (point: Position) => `${point.x.toFixed(2)} ${point.y.toFixed(2)}`
    return reverse ? `M ${p(b)} C ${p(c2)} ${p(c1)} ${p(a)}` : `M ${p(a)} C ${p(c1)} ${p(c2)} ${p(b)}`
  }

  return {
    positions, step, path,
    setBoundary: (check: typeof contains) => { contains = check },
    pin: (index: number, point: Position, scale: number) => {
      pin = { index, point: confine(index, point, Math.min(GRAPH_MOTION.dragRadius, GRAPH_MOTION.dragScreenRadius / scale), true) }
      Object.assign(positions[index], pin.point, { vx: 0, vy: 0 })
    },
    release: () => { pin = null },
  }
}
