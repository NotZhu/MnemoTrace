import type { GraphData } from '../../types'

export interface QuartzGraphSettings {
  nodeScale: number
  lineWidth: number
  repelForce: number
  centerForce: number
  linkDistance: number
  linkForce: number
  showRelations: boolean
  opacityScale: number
}

export const DEFAULT_QUARTZ_SETTINGS: QuartzGraphSettings = {
  nodeScale: 1.4, lineWidth: 0.8, repelForce: 1, centerForce: 0.2,
  linkDistance: 110, linkForce: 0.6, showRelations: false, opacityScale: 3,
}

export interface QuartzGraphView {
  signature: string
  nodes: Array<{ id: string; x: number; y: number }>
  width: number
  height: number
  transform: { k: number; x: number; y: number }
}

export interface QuartzGraphViewCache {
  get(signature: string): QuartzGraphView | undefined
  set(view: QuartzGraphView): void
}

export interface QuartzGraphController {
  setData(data: GraphData): void
  setLiveOnly(liveOnly: boolean): void
  setSelected(id: string | null): void
  setFocus(id: string | null): void
  configure(settings: QuartzGraphSettings): void
  resize(width: number, height: number): void
  zoomBy(factor: number): void
  panBy(x: number, y: number): void
  fit(): void
  reset(): void
  destroy(): void
}
