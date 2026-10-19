/*
 * Adapted from Quartz graph.inline.ts, MIT License, Copyright (c) 2021 jackyzha0.
 * Upstream revision and full license: ../../../vendor/quartz/.
 */
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3'
import type { Simulation, SimulationLinkDatum, SimulationNodeDatum } from 'd3'
import type { GraphData, GraphEdge, GraphNode } from '../../types'
import type { QuartzGraphSettings } from './types'

export interface QuartzNode extends GraphNode, SimulationNodeDatum { radius: number }
export interface QuartzLink extends SimulationLinkDatum<QuartzNode> {
  source: QuartzNode
  target: QuartzNode
  record: GraphEdge
}
export interface QuartzModel { nodes: QuartzNode[]; links: QuartzLink[]; forceLinks: QuartzLink[] }

export function createQuartzModel(data: GraphData, previous: QuartzNode[] = []): QuartzModel {
  const positions = new Map(previous.map((node) => [node.id, node]))
  const nodes = data.nodes.map((node) => {
    const old = positions.get(node.id)
    return { ...node, radius: 2, ...(old ? { x: old.x, y: old.y, vx: old.vx, vy: old.vy } : {}) }
  })
  const index = new Map(nodes.map((node) => [node.id, node]))
  const neighbors = new Map(nodes.map((node) => [node.id, new Set<string>()]))
  const links = data.edges.flatMap((record) => {
    const source = index.get(record.source), target = index.get(record.target)
    if (!source || !target) return []
    if (source !== target) { neighbors.get(source.id)!.add(target.id); neighbors.get(target.id)!.add(source.id) }
    return [{ source, target, record: { ...record } }]
  })
  for (const node of nodes) node.radius = 2 + Math.sqrt(neighbors.get(node.id)!.size)
  const seen = new Set<string>()
  // 同一对实体的多条事实全部绘制，只在弹簧计算中合并，避免重复事实改变牵引强度。
  const forceLinks = links.filter(({ source, target }) => {
    if (source === target) return false
    const key = JSON.stringify([source.id, target.id].sort())
    if (seen.has(key)) return false
    seen.add(key); return true
  })
  return { nodes, links, forceLinks }
}

export function configureQuartzForces(simulation: Simulation<QuartzNode, QuartzLink>, model: QuartzModel,
  settings: QuartzGraphSettings) {
  simulation
    .force('charge', forceManyBody<QuartzNode>().strength(-100 * settings.repelForce))
    .force('center', forceCenter<QuartzNode>().strength(settings.centerForce))
    // 平移质心不能收拢互不相连的组；弱轴向吸引让中心强度也能控制图谱的紧凑程度。
    .force('x', forceX<QuartzNode>(0).strength(settings.centerForce * 0.1))
    .force('y', forceY<QuartzNode>(0).strength(settings.centerForce * 0.1))
    .force('link', forceLink<QuartzNode, QuartzLink>(model.forceLinks).distance(settings.linkDistance).strength(settings.linkForce))
    .force('collide', forceCollide<QuartzNode>((node) => node.radius * settings.nodeScale).iterations(3))
}

export function createQuartzSimulation(model: QuartzModel, settings: QuartzGraphSettings) {
  const simulation = forceSimulation<QuartzNode>(model.nodes).stop()
  configureQuartzForces(simulation, model, settings)
  return simulation
}
