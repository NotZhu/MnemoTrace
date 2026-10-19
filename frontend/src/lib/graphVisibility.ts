import type { GraphData } from '../types'

// 显隐独立于布局数据，页面统计、详情和画布使用相同过滤规则。
export function visibleGraph(data: GraphData, liveOnly: boolean): GraphData {
  if (!liveOnly) return data
  const edges = data.edges.filter(edge => edge.live)
  const ids = new Set(edges.flatMap(edge => [edge.source, edge.target]))
  return { nodes: data.nodes.filter(node => ids.has(node.id)), edges }
}
