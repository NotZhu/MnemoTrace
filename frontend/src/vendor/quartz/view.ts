import type { GraphData } from '../../types'
import type { QuartzGraphSettings, QuartzGraphView, QuartzGraphViewCache } from './types'

export function graphViewSignature(data: GraphData, settings: QuartzGraphSettings): string {
  const nodes = data.nodes.map(node => JSON.stringify([node.id, node.name, node.type])).sort()
  const edges = data.edges.map(edge => JSON.stringify([edge.source, edge.target, edge.predicate, edge.live, edge.fact_id])).sort()
  return JSON.stringify([nodes, edges, settings.nodeScale, settings.repelForce, settings.centerForce, settings.linkDistance, settings.linkForce])
}

// 只保存在当前页面运行的内存中，刷新即清空；限制数量，避免搜索产生无限视图缓存。
const views = new Map<string, QuartzGraphView>()
export function graphViewCache(scope: string): QuartzGraphViewCache {
  const key = (signature: string) => JSON.stringify([scope, signature])
  return {
    get(signature) {
      const id = key(signature), view = views.get(id)
      if (view) { views.delete(id); views.set(id, view) }
      return view
    },
    set(view) {
      const id = key(view.signature)
      views.delete(id); views.set(id, view)
      while (views.size > 24) views.delete(views.keys().next().value!)
    },
  }
}
