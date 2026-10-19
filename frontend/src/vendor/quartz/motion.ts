import type { Simulation } from 'd3'
import type { QuartzLink, QuartzNode } from './model'

export interface QuartzEntrance {
  frames: Float64Array[]
  duration: number
}

// 用实际 D3 轨迹预先确定按钮操作的终点，避免播放结束后再次移动相机。
export function prepareEntrance(simulation: Simulation<QuartzNode, QuartzLink>, nodes: QuartzNode[]): QuartzEntrance {
  const capture = () => Float64Array.from(nodes.flatMap(node => [node.x ?? 0, node.y ?? 0]))
  for (const node of nodes) {
    node.x = (node.x ?? 0) * 0.88; node.y = (node.y ?? 0) * 0.88
    node.vx = 0; node.vy = 0
  }
  simulation.stop().alphaTarget(0).alpha(0.35)
  const frames = [capture()]
  const ticks = Math.max(0, Math.ceil(Math.log(simulation.alphaMin() / 0.35) / Math.log(1 - simulation.alphaDecay())))
  for (let tick = 0; tick < ticks; tick++) { simulation.tick(); frames.push(capture()) }
  return { frames, duration: ticks * 1000 / 60 }
}

export function applyEntrance(nodes: QuartzNode[], entrance: QuartzEntrance, elapsed: number): boolean {
  const last = entrance.frames.length - 1
  const step = entrance.duration ? Math.min(last, Math.max(0, elapsed) / entrance.duration * last) : last
  const index = Math.floor(step), blend = step - index
  const from = entrance.frames[index], to = entrance.frames[Math.min(last, index + 1)]
  nodes.forEach((node, i) => {
    node.x = from[i * 2] + (to[i * 2] - from[i * 2]) * blend
    node.y = from[i * 2 + 1] + (to[i * 2 + 1] - from[i * 2 + 1]) * blend
    node.vx = 0; node.vy = 0
  })
  return step === last
}
