import { useEffect, useLayoutEffect, useRef, type PointerEvent, type MouseEvent } from 'react'
import { nodes, edges } from './loginMemoryGraphData'
import { createGraphMotion, type Position } from './loginMemoryGraphMotion'

type Drag = { id: number; index: number; start: Position; grab: Position; moved: boolean }

export function useLoginMemoryGraphMotion(
  contour: string, paused: boolean, reducedMotion: boolean,
  activate: (index: number) => void, setDragging: (dragging: boolean) => void,
  setHoverNode: (index: number) => void,
  hoverNode: number,
) {
  const svgRef = useRef<SVGSVGElement>(null)
  const worldRef = useRef<SVGGElement>(null)
  const pressStart = useRef<{ id: number; x: number; y: number } | null>(null)
  const hovered = useRef(-1)
  const motionRef = useRef<ReturnType<typeof createGraphMotion> | null>(null)
  if (!motionRef.current) motionRef.current = createGraphMotion()
  const motion = motionRef.current
  const drag = useRef<Drag | null>(null)
  const suppressClick = useRef(false)
  const paintTargets = useRef<{ nodes: SVGGElement[]; edges: SVGPathElement[]; lights: SVGCircleElement[] }>({ nodes: [], edges: [], lights: [] })

  const paint = () => {
    paintTargets.current.nodes.forEach(element => {
      const index = Number(element.dataset.nodeIndex), point = motion.positions[index], home = nodes[index]
      element.setAttribute('transform', `translate(${(point.x - home.x).toFixed(2)} ${(point.y - home.y).toFixed(2)})`)
    })
    paintTargets.current.edges.forEach(element => element.setAttribute('d', motion.path(edges[Number(element.dataset.edgeIndex)], element.dataset.reverse === 'true', !reducedMotion)))
    paintTargets.current.lights.forEach(element => {
      const point = motion.positions[Number(element.dataset.nodeIndex)]
      element.setAttribute('cx', point.x.toFixed(2)); element.setAttribute('cy', point.y.toFixed(2))
    })
  }

  // React 只负责挂载/换电流计划，逐帧更新局部 SVG 属性，避免整个登录页每帧重渲染。
  useLayoutEffect(() => {
    hovered.current = hoverNode
    const svg = svgRef.current
    if (!svg) return
    paintTargets.current = {
      nodes: Array.from(svg.querySelectorAll<SVGGElement>('.login-graph-node')),
      edges: Array.from(svg.querySelectorAll<SVGPathElement>('path[data-edge-index]')),
      lights: Array.from(svg.querySelectorAll<SVGCircleElement>('circle[data-node-index]')),
    }
    paint()
  })

  useEffect(() => {
    const context = document.createElement('canvas').getContext('2d')!
    const outline = new Path2D(contour)
    const inside = (point: Position, margin: number) => [0, 1, 2, 3, 4, 5, 6, 7].every(index => {
      const angle = index * Math.PI / 4
      return context.isPointInPath(outline, point.x + Math.cos(angle) * margin, point.y + Math.sin(angle) * margin)
    })
    const margins = nodes.map(node => inside(node, node.radius + 3) ? node.radius + 3 : 0)
    motion.setBoundary((index, point) => inside(nodes[index], margins[index])
      ? inside(point, margins[index]) : Math.hypot(point.x - nodes[index].x, point.y - nodes[index].y) < 0.01)
    return () => motion.setBoundary(null)
  }, [contour, motion])

  useEffect(() => {
    if (paused) {
      const current = drag.current
      drag.current = null; motion.release(); setDragging(false)
      hovered.current = -1; setHoverNode(-1)
      pressStart.current = null
      if (current && svgRef.current?.hasPointerCapture(current.id)) svgRef.current.releasePointerCapture(current.id)
      return
    }
    let frameId = 0, previous = performance.now()
    const frame = (now: number) => {
      const dt = Math.min(now - previous, 40)
      motion.step(dt, !reducedMotion); previous = now
      paint()
      frameId = window.requestAnimationFrame(frame)
    }
    frameId = window.requestAnimationFrame(frame)
    return () => window.cancelAnimationFrame(frameId)
    // paint reads freshly collected SVG layers; activate/setDragging never enter the animation loop.
  }, [paused, reducedMotion, motion])

  const local = (clientX: number, clientY: number) => {
    // 漂移所在 g 的矩阵包含实际缩放/位移，缩放窗口后仍能抓住移动中的节点。
    const matrix = worldRef.current?.getScreenCTM()
    if (!matrix) return null
    const point = new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse())
    return { point, scale: Math.hypot(matrix.a, matrix.b) }
  }
  const pick = (point: Position, scale: number) => {
    let closest = -1, distance = Infinity
    motion.positions.forEach((node, index) => {
      const value = Math.hypot(node.x - point.x, node.y - point.y)
      if (value < distance) { closest = index; distance = value }
    })
    return distance * scale <= 22 ? closest : -1
  }
  const onPointerDown = (event: PointerEvent<SVGSVGElement>) => {
    if (event.button !== 0 || drag.current || paused) return
    suppressClick.current = false
    pressStart.current = { id: event.pointerId, x: event.clientX, y: event.clientY }
    const position = local(event.clientX, event.clientY)
    if (!position) return
    const index = pick(position.point, position.scale)
    if (index < 0) return
    const node = motion.positions[index]
    drag.current = {
      id: event.pointerId, index, moved: false, start: { x: event.clientX, y: event.clientY },
      grab: { x: position.point.x - node.x, y: position.point.y - node.y },
    }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const current = drag.current
    if (!current) {
      const start = pressStart.current
      // 空白处不捕获拖拽，也不平移；移动过的按压不能误触结束位置的节点。
      if (start?.id === event.pointerId && Math.hypot(event.clientX - start.x, event.clientY - start.y) >= 6) suppressClick.current = true
      if (event.pointerType !== 'touch') {
        const position = local(event.clientX, event.clientY)
        const index = position ? pick(position.point, position.scale) : -1
        if (hovered.current !== index) { hovered.current = index; setHoverNode(index) }
      }
      return
    }
    if (current.id !== event.pointerId) return
    if (!current.moved && Math.hypot(event.clientX - current.start.x, event.clientY - current.start.y) < 6) return
    const position = local(event.clientX, event.clientY)
    if (!position) return
    if (!current.moved) { current.moved = true; setDragging(true); hovered.current = -1; setHoverNode(-1) }
    suppressClick.current = true
    event.preventDefault()
    motion.pin(current.index, { x: position.point.x - current.grab.x, y: position.point.y - current.grab.y }, position.scale)
    paint()
  }
  const endDrag = (event: PointerEvent<SVGSVGElement>) => {
    if (pressStart.current?.id === event.pointerId) pressStart.current = null
    const current = drag.current
    if (!current || current.id !== event.pointerId) return
    drag.current = null; motion.release(); setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  const onPointerCancel = (event: PointerEvent<SVGSVGElement>) => {
    if (pressStart.current?.id === event.pointerId) { pressStart.current = null; suppressClick.current = true }
    if (!drag.current || drag.current.id !== event.pointerId) return
    suppressClick.current = true
    endDrag(event)
  }
  const onClick = (event: MouseEvent<SVGSVGElement>) => {
    if (suppressClick.current) { suppressClick.current = false; return }
    const position = local(event.clientX, event.clientY)
    if (!position) return
    const index = pick(position.point, position.scale)
    if (index >= 0) activate(index)
  }
  return {
    positions: motion.positions, svgRef, worldRef,
    handlers: {
      onPointerDown, onPointerMove, onPointerUp: endDrag, onPointerCancel, onLostPointerCapture: onPointerCancel, onClick,
      onPointerLeave: () => { hovered.current = -1; setHoverNode(-1) },
    },
  }
}
