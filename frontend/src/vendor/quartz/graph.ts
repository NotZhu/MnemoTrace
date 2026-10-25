/*
 * Adapted from Quartz graph.inline.ts, MIT License, Copyright (c) 2021 jackyzha0.
 * Upstream: d25a6eabf96751ffca56f8a8139272def7a65041.
 * See ../../../vendor/quartz/LICENSE.txt and README.md for provenance and changes.
 */
import { drag, dragEnable, select, zoom, zoomIdentity } from 'd3'
import type { D3DragEvent, ZoomTransform } from 'd3'
import { Application, Circle, Container, Graphics, Text } from 'pixi.js'
// 服务端 CSP 禁止动态 Function；使用 Pixi 的静态 shader/uniform 同步实现。
import 'pixi.js/unsafe-eval'
import { Group as TweenGroup, Tween } from '@tweenjs/tween.js'
import type { GraphData } from '../../types'
import { configureQuartzForces, createQuartzModel, createQuartzSimulation } from './model'
import type { QuartzLink, QuartzNode } from './model'
import { DEFAULT_QUARTZ_SETTINGS } from './types'
import type { QuartzGraphController, QuartzGraphSettings, QuartzGraphView, QuartzGraphViewCache } from './types'
import { graphViewSignature } from './view'
import { visibleGraph } from '../../lib/graphVisibility'
import { applyEntrance, prepareEntrance } from './motion'
import type { QuartzEntrance } from './motion'

interface NodeRenderData {
  node: QuartzNode; gfx: Graphics; label: Text
  overviewName: string; overviewWidth: number
  alpha: number; labelAlpha: number
}
interface LinkRenderData { link: QuartzLink; gfx: Graphics; label: Text; alpha: number }

export async function createQuartzGraph(host: HTMLElement, data: GraphData, options: {
  settings: QuartzGraphSettings
  selectedId: string | null
  onSelect(id: string | null): void
  signal: AbortSignal
  liveOnly?: boolean
  viewCache?: QuartzGraphViewCache
}): Promise<QuartzGraphController> {
  let size = { width: Math.max(1, host.clientWidth), height: Math.max(1, host.clientHeight) }
  let settings = { ...options.settings }, currentData = data
  let liveOnly = options.liveOnly ?? false
  let visibleNodeIds = new Set(visibleGraph(data, liveOnly).nodes.map(node => node.id))
  let selectedId = options.selectedId, hoveredId: string | null = null, focusId: string | null = null
  let disposed = false, dragging = false
  let mounted = false
  let hasLayout = false
  let frame: number | null = null
  let transform = zoomIdentity
  const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
  const tweens = new TweenGroup()
  let entrancePositions: QuartzGraphView['nodes'] | null = null
  let playback: { entrance: QuartzEntrance; startedAt: number } | null = null
  const app = new Application()
  try {
    await app.init({ width: size.width, height: size.height, antialias: true, autoStart: false, autoDensity: true,
      backgroundAlpha: 0, preference: 'webgl', resolution: Math.min(window.devicePixelRatio || 1, 2), eventMode: 'static' })
  } catch (error) {
    if (app.renderer) app.destroy(true, { children: true })
    throw error
  }
  if (options.signal.aborted) { app.destroy(true, { children: true }); throw new DOMException('Cancelled', 'AbortError') }
  const canvas = app.canvas
  canvas.setAttribute('aria-hidden', 'true')
  host.appendChild(canvas)
  const stage = app.stage
  const linkContainer = new Container({ zIndex: 1, isRenderGroup: true })
  const nodesContainer = new Container({ zIndex: 2, isRenderGroup: true })
  const labelsContainer = new Container({ zIndex: 3, isRenderGroup: true })
  labelsContainer.sortableChildren = true
  stage.sortableChildren = true
  stage.addChild(linkContainer, nodesContainer, labelsContainer)
  const nodeRenderData: NodeRenderData[] = [], linkRenderData: LinkRenderData[] = []
  let model = createQuartzModel(data)
  const simulation = createQuartzSimulation(model, settings)
  const surface = select<HTMLCanvasElement, unknown>(canvas)

  function readPalette() {
    const style = getComputedStyle(host)
    const color = (name: string, fallback: string) => {
      const value = style.getPropertyValue(name).trim()
      return value ? `rgb(${value.split(/\s+/).join(',')})` : fallback
    }
    return { node: color('--graph-node', '#9baeca'), link: color('--graph-link', '#718096'),
      text: color('--graph-text', '#9baeca'), active: color('--graph-active', '#c3d4eb'),
      foreground: color('--memory-text', '#edf3fa'), fontFamily: style.fontFamily }
  }
  let palette = readPalette()

  function requestRender() {
    if (!disposed && frame === null) frame = requestAnimationFrame(render)
  }

  function activeInfo() {
    const candidate = hoveredId ?? focusId ?? selectedId
    const id = candidate && visibleNodeIds.has(candidate) ? candidate : null
    const neighbors = new Set<string>(id ? [id] : [])
    if (id) for (const { source, target, record } of model.links) {
      if (liveOnly && !record.live) continue
      if (source.id === id || target.id === id) { neighbors.add(source.id); neighbors.add(target.id) }
    }
    return { id, neighbors }
  }

  function updateAppearance() {
    tweens.removeAll()
    const { id, neighbors } = activeInfo()
    const labelOpacity = Math.max(0, Math.min(1, (transform.k * settings.opacityScale - 1.25) / 2.5))
    for (const item of nodeRenderData) {
      const active = item.node.id === id, neighbor = !!id && neighbors.has(item.node.id)
      const alpha = !id || active ? 1 : neighbor ? 0.9 : 0.12
      const labelAlpha = active ? 1 : neighbor ? Math.max(0.78, labelOpacity) : id ? labelOpacity * 0.08 : labelOpacity
      const text = active ? item.node.name : item.overviewName
      if (item.label.text !== text) item.label.text = text
      item.label.zIndex = active ? 2 : neighbor ? 1 : 0
      if (preference.matches) { item.alpha = alpha; item.labelAlpha = labelAlpha }
      else new Tween(item, tweens).to({ alpha, labelAlpha }, 180).start()
    }
    for (const item of linkRenderData) {
      const connected = item.link.source.id === id || item.link.target.id === id
      const alpha = !id ? 0.55 : connected ? 0.95 : 0.06
      if (preference.matches) item.alpha = alpha
      else new Tween(item, tweens).to({ alpha }, 180).start()
    }
    requestRender()
  }

  function dashedLine(gfx: Graphics, ax: number, ay: number, bx: number, by: number) {
    const distance = Math.hypot(bx - ax, by - ay)
    if (!distance) return
    const dash = 4 / transform.k, step = 9 / transform.k
    for (let d = 0; d < distance; d += step) {
      const end = Math.min(distance, d + dash)
      gfx.moveTo(ax + (bx - ax) * d / distance, ay + (by - ay) * d / distance)
        .lineTo(ax + (bx - ax) * end / distance, ay + (by - ay) * end / distance)
    }
  }

  function render(time: number) {
    frame = null
    if (disposed) return
    if (playback && applyEntrance(model.nodes, playback.entrance, time - playback.startedAt)) {
      playback = null; entrancePositions = null
    }
    tweens.update(time, false)
    const { id, neighbors } = activeInfo()
    for (const item of nodeRenderData) {
      const { node, gfx, label } = item
      gfx.visible = label.visible = visibleNodeIds.has(node.id)
      if (!gfx.visible) continue
      const x = (node.x ?? 0) + size.width / 2, y = (node.y ?? 0) + size.height / 2
      const radius = node.radius * settings.nodeScale
      const active = node.id === id
      gfx.clear().circle(0, 0, radius).fill({ color: active ? palette.active : palette.node })
      gfx.position.set(x, y); gfx.alpha = item.alpha
      gfx.hitArea = new Circle(0, 0, Math.max(radius, 10 / transform.k))
      label.position.set(x, y - radius - 3 / transform.k)
      label.scale.set((active ? 1.1 : 1) / transform.k)
      label.alpha = item.labelAlpha
      label.style.fill = id && neighbors.has(node.id) ? palette.foreground : palette.text
    }
    for (const item of linkRenderData) {
      const { link, gfx, label } = item
      gfx.visible = !liveOnly || link.record.live
      label.visible = gfx.visible && settings.showRelations
      if (!gfx.visible) continue
      const ax = (link.source.x ?? 0) + size.width / 2, ay = (link.source.y ?? 0) + size.height / 2
      const bx = (link.target.x ?? 0) + size.width / 2, by = (link.target.y ?? 0) + size.height / 2
      const active = id === link.source.id || id === link.target.id
      gfx.clear()
      if (link.source === link.target) gfx.circle(ax, ay - 17, 17)
      else if (link.record.live) gfx.moveTo(ax, ay).lineTo(bx, by)
      else dashedLine(gfx, ax, ay, bx, by)
      gfx.stroke({ width: settings.lineWidth * (active ? 1.5 : 1) / transform.k,
        color: active ? palette.active : palette.link, alpha: item.alpha })
      label.position.set((ax + bx) / 2, (ay + by) / 2 - 7 / transform.k)
      label.scale.set(1 / transform.k); label.alpha = item.alpha
      label.style.fill = palette.text
    }
    app.renderer.render(stage)
    if (playback || tweens.getAll().length) requestRender()
  }

  function makeLabel(text: string, fontSize: number) {
    return new Text({ text, eventMode: 'none', anchor: { x: 0.5, y: 1 },
      style: { fontSize, fontFamily: palette.fontFamily, fill: palette.text },
      resolution: Math.min(window.devicePixelRatio || 1, 2) * 2 })
  }

  function compactLabel(label: Text) {
    const characters = Array.from(label.text)
    if (label.getLocalBounds().width <= 120) return label.text
    let low = 0, high = characters.length
    while (low < high) {
      const middle = Math.ceil((low + high) / 2)
      label.text = characters.slice(0, middle).join('') + '…'
      if (label.getLocalBounds().width <= 120) low = middle
      else high = middle - 1
    }
    label.text = characters.slice(0, low).join('') + '…'
    return label.text
  }

  function saveView(visiblePositions = false) {
    if (!mounted || !options.viewCache) return
    options.viewCache.set({ signature: graphViewSignature(currentData, settings),
      nodes: (!visiblePositions && entrancePositions) || model.nodes.map(node => ({ id: node.id, x: node.x ?? 0, y: node.y ?? 0 })),
      width: size.width, height: size.height, transform: { k: transform.k, x: transform.x, y: transform.y } })
  }

  function startEntrance() {
    if (preference.matches || model.nodes.length < 2) return
    // 首次与切页返回共用自然受力入场，相机保持不动。
    // 入场中离开仍保存收拢前的基准，避免快速切页反复压缩布局。
    entrancePositions = model.nodes.map(node => ({ id: node.id, x: node.x ?? 0, y: node.y ?? 0 }))
    for (const node of model.nodes) {
      node.x = (node.x ?? 0) * 0.88; node.y = (node.y ?? 0) * 0.88
      node.vx = 0; node.vy = 0
    }
    simulation.alphaTarget(0).alpha(0.35).restart()
  }

  function cancelPlayback() {
    playback = null
    entrancePositions = null
  }

  function mountModel(next: GraphData, {
    preservePositions = true, restoreView = true, planEntrance = false, preserveView = false,
  } = {}) {
    preserveView = preserveView && hasLayout
    const previousIds = new Set(preservePositions ? model.nodes.map(node => node.id) : [])
    const saved = restoreView ? options.viewCache?.get(graphViewSignature(next, settings)) : undefined
    mounted = false
    playback = null
    simulation.stop()
    tweens.removeAll()
    entrancePositions = null
    for (const container of [linkContainer, nodesContainer, labelsContainer]) {
      for (const child of container.removeChildren()) child.destroy({ children: true })
    }
    nodeRenderData.length = 0; linkRenderData.length = 0
    model = createQuartzModel(next, preservePositions ? model.nodes : [])
    hasLayout = preserveView ? hasLayout : model.nodes.length > 0
    // 已显示节点优先使用当前位置；重新出现的节点复用该结果的有界视图缓存。
    const restoredIds = new Set<string>()
    if (preserveView && saved) {
      const positions = new Map(saved.nodes.map(node => [node.id, node]))
      for (const node of model.nodes) {
        const position = positions.get(node.id)
        if (!previousIds.has(node.id) && position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
          node.x = position.x; node.y = position.y
          restoredIds.add(node.id)
        }
      }
    }
    visibleNodeIds = new Set(visibleGraph(next, liveOnly).nodes.map(node => node.id))
    simulation.nodes(model.nodes)
    configureQuartzForces(simulation, model, settings)
    hoveredId = null; focusId = null; canvas.title = ''
    for (const node of model.nodes) {
      const gfx = new Graphics({ eventMode: 'static', label: node.id, cursor: 'pointer', hitArea: new Circle(0, 0, 12) })
      const label = makeLabel(node.name, 12)
      const overviewName = compactLabel(label), overviewWidth = label.getLocalBounds().width
      gfx.on('pointerover', () => { if (!dragging) { hoveredId = node.id; canvas.title = node.name; updateAppearance() } })
      gfx.on('pointerleave', () => { if (!dragging) { hoveredId = null; canvas.title = ''; updateAppearance() } })
      nodesContainer.addChild(gfx); labelsContainer.addChild(label)
      nodeRenderData.push({ node, gfx, label, overviewName, overviewWidth, alpha: 1, labelAlpha: 1 })
    }
    for (const link of model.links) {
      const gfx = new Graphics({ eventMode: 'none' }), label = makeLabel(link.record.predicate, 11)
      linkContainer.addChild(gfx); labelsContainer.addChild(label)
      linkRenderData.push({ link, gfx, label, alpha: 1 })
    }
    if (preserveView) {
      const known = model.nodes.filter(node => previousIds.has(node.id) || restoredIds.has(node.id))
      if (known.length < model.nodes.length) {
        // 仅为新节点求位置；临时固定已有节点，避免过滤导致整图跳动。
        for (const node of known) { node.fx = node.x; node.fy = node.y }
        const ticks = Math.ceil(Math.log(simulation.alphaMin()) / Math.log(1 - simulation.alphaDecay()))
        simulation.alphaTarget(0).alpha(1).tick(ticks)
        for (const node of known) { delete node.fx; delete node.fy }
      }
      for (const node of model.nodes) { node.vx = 0; node.vy = 0 }
      simulation.alphaTarget(0).alpha(0).stop()
      mounted = true
      updateAppearance()
      return
    }
    if (saved && saved.signature === graphViewSignature(next, settings) && saved.nodes.length === model.nodes.length && saved.width > 0 && saved.height > 0 &&
      [saved.width, saved.height, saved.transform.k, saved.transform.x, saved.transform.y].every(Number.isFinite) && saved.transform.k >= 0.2 && saved.transform.k <= 4) {
      const positions = new Map(saved.nodes.map(node => [node.id, node]))
      if (model.nodes.every(node => { const position = positions.get(node.id); return position && Number.isFinite(position.x) && Number.isFinite(position.y) })) {
        for (const node of model.nodes) {
          const position = positions.get(node.id)!
          node.x = position.x; node.y = position.y; node.vx = 0; node.vy = 0
        }
        simulation.alphaTarget(0).alpha(0).stop()
        const x = saved.transform.x + (size.width - saved.width) / 2 * (1 - saved.transform.k)
        const y = saved.transform.y + (size.height - saved.height) / 2 * (1 - saved.transform.k)
        surface.call(zoomBehavior.transform, zoomIdentity.translate(x, y).scale(saved.transform.k))
        startEntrance()
        mounted = true
        updateAppearance()
        return
      }
    }
    // 首次显示前求解至冷却阈值，避免先展示中间布局、结束后又缩小视口。
    const initialTicks = Math.ceil(Math.log(simulation.alphaMin()) / Math.log(1 - simulation.alphaDecay()))
    simulation.alphaTarget(0).alpha(1).tick(initialTicks)
    if (planEntrance && !preference.matches && model.nodes.length > 1) {
      entrancePositions = model.nodes.map(node => ({ id: node.id, x: node.x ?? 0, y: node.y ?? 0 }))
      const entrance = prepareEntrance(simulation, model.nodes)
      fitViewport()
      playback = { entrance, startedAt: performance.now() }
      applyEntrance(model.nodes, entrance, 0)
    } else {
      fitViewport()
      startEntrance()
    }
    mounted = true
    updateAppearance()
  }

  function pickNode(x: number, y: number) {
    const world = transform.invert([x, y])
    let node: QuartzNode | undefined, nearest = 16 / transform.k
    for (const candidate of model.nodes) {
      if (!visibleNodeIds.has(candidate.id)) continue
      const distance = Math.hypot((candidate.x ?? 0) + size.width / 2 - world[0], (candidate.y ?? 0) + size.height / 2 - world[1])
      if (distance < nearest) { node = candidate; nearest = distance }
    }
    return node && Math.hypot((node.x ?? 0) + size.width / 2 - world[0], (node.y ?? 0) + size.height / 2 - world[1]) <=
      Math.max(node.radius * settings.nodeScale, 10 / transform.k) ? node : undefined
  }

  let dragOrigin: { x: number; y: number; dx: number; dy: number } | null = null
  type DragEvent = D3DragEvent<HTMLCanvasElement, unknown, QuartzNode>
  surface.call(drag<HTMLCanvasElement, unknown, QuartzNode>()
    .container(() => canvas)
    .subject((event) => pickNode(event.x, event.y)!)
    .on('start', (event: DragEvent) => {
      cancelPlayback()
      dragging = true
      const node = event.subject
      node.fx = node.x; node.fy = node.y
      dragOrigin = { x: node.x ?? 0, y: node.y ?? 0, dx: 0, dy: 0 }
      canvas.style.cursor = 'grabbing'
      if (!preference.matches) simulation.alphaTarget(0.3).restart()
    })
    .on('drag', (event: DragEvent) => {
      if (!dragOrigin) return
      dragOrigin.dx = event.x - dragOrigin.x; dragOrigin.dy = event.y - dragOrigin.y
      event.subject.fx = dragOrigin.x + dragOrigin.dx / transform.k
      event.subject.fy = dragOrigin.y + dragOrigin.dy / transform.k
      if (preference.matches) { event.subject.x = event.subject.fx; event.subject.y = event.subject.fy }
      requestRender()
    })
    .on('end', (event: DragEvent) => {
      simulation.alphaTarget(0)
      event.subject.fx = null; event.subject.fy = null
      dragging = false; canvas.style.cursor = ''
      if (dragOrigin && Math.hypot(dragOrigin.dx, dragOrigin.dy) <= 4) options.onSelect(event.subject.id)
      dragOrigin = null; hoveredId = null
      updateAppearance()
    }))

  const zoomBehavior = zoom<HTMLCanvasElement, unknown>()
    .extent(() => [[0, 0], [size.width, size.height]])
    .scaleExtent([0.2, 4])
    .on('zoom', ({ transform: next }: { transform: ZoomTransform }) => {
      transform = next
      stage.scale.set(next.k); stage.position.set(next.x, next.y)
      for (const item of nodeRenderData) item.gfx.hitArea = new Circle(0, 0, Math.max(item.node.radius * settings.nodeScale, 10 / next.k))
      updateAppearance()
    })
  surface.call(zoomBehavior).on('dblclick.zoom', null)
  const backgroundClick = (event: MouseEvent) => {
    if (event.defaultPrevented || dragging) return
    const bounds = canvas.getBoundingClientRect()
    if (!pickNode(event.clientX - bounds.left, event.clientY - bounds.top)) options.onSelect(null)
  }
  canvas.addEventListener('click', backgroundClick)
  canvas.addEventListener('mousedown', focusCanvas)
  function focusCanvas() { host.focus({ preventScroll: true }) }

  function fitViewport() {
    if (!model.nodes.length) return
    let scale = 1, left = 0, right = 0, top = 0, bottom = 0
    for (let i = 0; i < 4; i++) {
      // 聚焦展开全名只影响阅读，不改变适配范围或挤小整张图。
      left = Math.min(...nodeRenderData.map(({ node, overviewWidth }) => (node.x ?? 0) - Math.max(node.radius * settings.nodeScale, overviewWidth / scale / 2)))
      right = Math.max(...nodeRenderData.map(({ node, overviewWidth }) => (node.x ?? 0) + Math.max(node.radius * settings.nodeScale, overviewWidth / scale / 2)))
      top = Math.min(...model.nodes.map((node) => (node.y ?? 0) - node.radius * settings.nodeScale - 23 / scale))
      bottom = Math.max(...model.nodes.map((node) => (node.y ?? 0) + node.radius * settings.nodeScale))
      scale = Math.max(0.2, Math.min(1.5, Math.max(1, size.width - 100) / Math.max(1, right - left),
        Math.max(1, size.height - 120) / Math.max(1, bottom - top)))
    }
    const next = zoomIdentity.translate(size.width / 2 - (size.width / 2 + (left + right) / 2) * scale,
      size.height / 2 - (size.height / 2 + (top + bottom) / 2) * scale).scale(scale)
    surface.call(zoomBehavior.transform, next)
  }

  simulation.on('tick.render', requestRender).on('end.render', () => {
    if (!playback) entrancePositions = null
    requestRender()
  })
  function motionPreference() {
    if (preference.matches) {
      if (playback) applyEntrance(model.nodes, playback.entrance, playback.entrance.duration)
      playback = null
      simulation.stop().alphaTarget(0); tweens.removeAll(); entrancePositions = null
      updateAppearance()
    }
    else requestRender()
  }
  preference.addEventListener('change', motionPreference)
  const themeObserver = new MutationObserver(() => {
    palette = readPalette()
    for (const item of [...nodeRenderData, ...linkRenderData]) item.label.style.fontFamily = palette.fontFamily
    requestRender()
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] })
  function destroy() {
    if (disposed) return
    saveView()
    disposed = true
    playback = null
    simulation.stop().on('tick.render', null).on('end.render', null)
    tweens.removeAll(); themeObserver.disconnect(); preference.removeEventListener('change', motionPreference)
    if (frame !== null) cancelAnimationFrame(frame)
    surface.on('.drag', null).on('.zoom', null)
    if (dragging) { select(window).on('mousemove.drag mouseup.drag', null); dragEnable(window, true) }
    canvas.removeEventListener('click', backgroundClick); canvas.removeEventListener('mousedown', focusCanvas)
    app.destroy(true, { children: true, texture: true, textureSource: true })
  }
  try { mountModel(data) } catch (error) { destroy(); throw error }

  return {
    setData(next, update) {
      if (graphViewSignature(next, settings) === graphViewSignature(currentData, settings)) { currentData = next; return }
      saveView(update?.preserveView); currentData = next
      mountModel(next, { preserveView: update?.preserveView ?? false })
    },
    setLiveOnly(next) {
      if (next === liveOnly) return
      liveOnly = next
      visibleNodeIds = new Set(visibleGraph(currentData, liveOnly).nodes.map(node => node.id))
      hoveredId = null; focusId = null; canvas.title = ''
      updateAppearance()
    },
    setSelected(id) { selectedId = id; updateAppearance() },
    setFocus(id) { focusId = id; updateAppearance() },
    configure(next) {
      const physicsChanged = ['nodeScale', 'repelForce', 'centerForce', 'linkDistance', 'linkForce'].some((key) =>
        settings[key as keyof QuartzGraphSettings] !== next[key as keyof QuartzGraphSettings])
      if (physicsChanged) saveView()
      settings = { ...next }
      if (physicsChanged) {
        cancelPlayback()
        configureQuartzForces(simulation, model, settings)
        if (preference.matches) simulation.stop().alpha(1).tick(300)
        else simulation.alpha(0.5).restart()
      }
      updateAppearance()
    },
    resize(width, height) {
      if (width < 1 || height < 1 || (width === size.width && height === size.height)) return
      cancelPlayback()
      size = { width, height }; app.renderer.resize(width, height)
      if (preference.matches) simulation.stop().alpha(1).tick(300)
      else simulation.alpha(0.3).restart()
      fitViewport()
    },
    zoomBy(factor) { surface.call(zoomBehavior.scaleBy, factor, [size.width / 2, size.height / 2]) },
    panBy(x, y) { surface.call(zoomBehavior.translateBy, x / transform.k, y / transform.k) },
    fit() { mountModel(currentData, { preservePositions: false, restoreView: false, planEntrance: true }) },
    reset() {
      settings = { ...DEFAULT_QUARTZ_SETTINGS }
      selectedId = null
      mountModel(currentData, { preservePositions: false, restoreView: false, planEntrance: true })
    },
    destroy,
  }
}
