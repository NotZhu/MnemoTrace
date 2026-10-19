import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'

import { nodes, edges, distances, ambientOrigins, createAmbientSignal, AMBIENT_INTERVAL_MS, AMBIENT_NODE_FADE_MS, type AmbientSignal } from './loginMemoryGraphData'
import { nodeColors, edgeStrokes } from './loginMemoryGraphAppearance'
import { useLoginMemoryGraphMotion } from './useLoginMemoryGraphMotion'

const STEP_MS = 500
const MAX_HOPS = 5
const WAVE_MS = MAX_HOPS * STEP_MS + 1200

const BRAIN_CONTOUR = 'M200 267 Q221 217 277 204 Q313 157 362 164 Q388 128 428 134 Q461 101 500 107 Q539 80 573 97 Q624 74 670 105 Q737 87 778 144 Q840 129 878 181 Q950 179 981 248 Q1048 263 1063 335 Q1127 355 1124 422 Q1183 450 1176 511 Q1215 555 1198 619 Q1227 681 1184 728 Q1206 785 1138 839 Q1091 878 1040 870 Q1075 919 1021 958 Q1006 1013 932 1018 Q870 1063 807 1000 Q787 978 766 981 Q765 1063 827 1140 Q843 1164 820 1162 Q783 1162 741 1083 Q698 1000 667 984 Q602 960 613 866 Q554 919 502 893 Q451 904 430 862 Q348 873 314 830 Q279 790 305 740 Q261 742 221 716 Q173 727 137 696 Q62 688 58 582 Q23 551 45 506 Q31 462 76 421 Q63 370 130 338 Q146 289 200 267 Z'

function BrainSurface({ id }: { id: string }) {
  const lobes = [
    [445, 157, 100, 84], [595, 137, 118, 90], [772, 180, 117, 92],
    [913, 255, 116, 94], [1031, 380, 97, 118], [1131, 547, 92, 114],
    [1122, 714, 88, 117], [941, 861, 124, 94], [863, 959, 143, 83], [429, 821, 149, 97],
  ]
  return (
    <g className="login-graph-surface" aria-hidden="true">
      <defs>
        <path id={`${id}-contour`} d={BRAIN_CONTOUR} />
        <clipPath id={`${id}-clip`}><use href={`#${id}-contour`} /></clipPath>
        <radialGradient id={`${id}-body`} cx="78%" cy="29%" r="77%">
          <stop stopColor="#405d82" stopOpacity="0.22" />
          <stop offset="0.56" stopColor="#202e43" stopOpacity="0.1" />
          <stop offset="1" stopColor="#12171f" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={`${id}-rim`} cx="80%" cy="25%" r="90%">
          <stop stopColor="#b3d6ff" stopOpacity="0.95" />
          <stop offset="0.48" stopColor="#8aacd2" stopOpacity="0.85" />
          <stop offset="0.7" stopColor="#556b86" stopOpacity="0.75" />
          <stop offset="1" stopColor="#2e3a49" stopOpacity="0.5" />
        </radialGradient>
        <radialGradient id={`${id}-lobe`}>
          <stop stopColor="#8bacdf" stopOpacity="0.13" />
          <stop offset="0.68" stopColor="#7495c4" stopOpacity="0.055" />
          <stop offset="1" stopColor="#7293c1" stopOpacity="0" />
        </radialGradient>
        <filter id={`${id}-soft`} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="3" />
        </filter>
      </defs>
      <use href={`#${id}-contour`} fill={`url(#${id}-body)`} stroke={`url(#${id}-rim)`} strokeWidth="6" opacity="0.4" filter={`url(#${id}-soft)`} />
      <use href={`#${id}-contour`} fill={`url(#${id}-body)`} stroke={`url(#${id}-rim)`} strokeWidth="3.3" />
      <g clipPath={`url(#${id}-clip)`}>
        <g fill={`url(#${id}-lobe)`}>
          {lobes.map(([cx, cy, rx, ry], index) => <ellipse key={index} cx={cx} cy={cy} rx={rx} ry={ry} />)}
        </g>
        <path
          d="M613 866 Q608 947 667 984 Q698 1000 741 1083 Q783 1162 820 1162 M1039 870 Q1001 866 978 852 Q925 825 877 842 Q821 865 779 861 Q711 856 709 914 Q707 965 766 981"
          stroke={`url(#${id}-rim)`} strokeWidth="2.2" opacity="0.45"
        />
      </g>
    </g>
  )
}

export function LoginMemoryGraph() {
  const surfaceId = `login-brain-${useId().replace(/:/g, '')}`
  const [focusNode, setFocusNode] = useState(0)
  const [wave, setWave] = useState<{ id: number; origin: number; depths: number[] } | null>(null)
  const [ambient, setAmbient] = useState<{ id: number; signal: AmbientSignal } | null>(null)
  const [dragging, setDragging] = useState(false)
  const [hoverNode, setHoverNode] = useState(-1)
  const [keyboardNode, setKeyboardNode] = useState(-1)
  const highlightNode = dragging ? -1 : hoverNode >= 0 ? hoverNode : keyboardNode
  const [paused, setPaused] = useState(() => typeof document !== 'undefined' && document.hidden)
  const [reducedMotion, setReducedMotion] = useState(() => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const sequence = useRef(0)
  const ambientCursor = useRef(0)
  const nodeRefs = useRef<(SVGGElement | null)[]>([])

  useEffect(() => {
    if (!wave) return
    const timer = window.setTimeout(() => setWave(null), WAVE_MS)
    return () => window.clearTimeout(timer)
  }, [wave])

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => { setPaused(document.hidden); setReducedMotion(query.matches) }
    document.addEventListener('visibilitychange', update)
    query.addEventListener('change', update)
    return () => {
      document.removeEventListener('visibilitychange', update)
      query.removeEventListener('change', update)
    }
  }, [])

  useEffect(() => {
    if (paused || reducedMotion || wave || dragging || !ambientOrigins.length) {
      setAmbient(null)
      return
    }
    let nextTimer: number
    let endTimer: number
    const emit = () => {
      const origin = ambientOrigins[ambientCursor.current++ % ambientOrigins.length]
      const signal = createAmbientSignal(origin)
      setAmbient({ id: ++sequence.current, signal })
      endTimer = window.setTimeout(() => setAmbient(null), signal.durationMs)
      nextTimer = window.setTimeout(emit, AMBIENT_INTERVAL_MS)
    }
    nextTimer = window.setTimeout(emit, 650)
    return () => { window.clearTimeout(nextTimer); window.clearTimeout(endTimer) }
  }, [paused, reducedMotion, wave, dragging])

  const activate = (origin: number) => {
    setAmbient(null)
    setFocusNode(origin)
    setWave({ id: ++sequence.current, origin, depths: distances(origin) })
  }

  const motion = useLoginMemoryGraphMotion(BRAIN_CONTOUR, paused, reducedMotion, activate, setDragging, setHoverNode, hoverNode)

  const keyboard = (event: KeyboardEvent<SVGGElement>, index: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      activate(index)
      return
    }
    const directions: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    }
    const direction = directions[event.key]
    if (!direction) return
    event.preventDefault()
    const current = motion.positions[index]
    const nearest = motion.positions.map((node, next) => {
      const dx = node.x - current.x, dy = node.y - current.y
      return { next, forward: dx * direction[0] + dy * direction[1], score: Math.hypot(dx, dy) + Math.abs(dx * direction[1] - dy * direction[0]) * 2 }
    }).filter(node => node.forward > 1).sort((a, b) => a.score - b.score)[0]
    if (nearest) {
      setFocusNode(nearest.next)
      nodeRefs.current[nearest.next]?.focus()
    }
  }

  return (
    <div className="login-memory-backdrop" data-paused={paused || undefined} data-active={!!wave || undefined} data-dragging={dragging || undefined}>
      <svg
        ref={motion.svgRef}
        className="login-memory-graph" viewBox="0 0 1254 1254" fill="none"
        role="group" aria-label={'记忆图谱。拖拽节点牵动邻居，松手回稳；悬停高亮连接，点击传递电流，方向键切换节点，回车激活。'}
        {...motion.handlers}
      >
        <g className="login-graph-drift" ref={motion.worldRef}>
          <BrainSurface id={surfaceId} />
          <g clipPath={`url(#${surfaceId}-clip)`}>
          <g className="login-graph-base-edges" aria-hidden="true">
            {edges.map((edge, index) => {
              const from = nodes[edge.from], to = nodes[edge.to]
              const key = [`${from.x},${from.y}`, `${to.x},${to.y}`].sort().join('|')
              return <path key={index} data-edge-index={index} d={edge.path} stroke={edgeStrokes[key]} />
            })}
          </g>
          {highlightNode >= 0 && (
            <g className="login-graph-hover-edges" aria-hidden="true">
              {edges.map((edge, index) => edge.from === highlightNode || edge.to === highlightNode
                ? <path key={index} data-edge-index={index} d={edge.path} /> : null)}
            </g>
          )}
          {ambient && (
            <g key={ambient.id} className="login-graph-ambient" data-origin={ambient.signal.origin} data-pulse={ambient.id} aria-hidden="true">
              {ambient.signal.paths.map((path, index) => (
                <path
                  key={index} className="login-graph-ambient-current"
                  data-edge-index={edges.indexOf(path.edge)} data-reverse={path.from !== path.edge.from}
                  d={path.from === path.edge.from ? path.edge.path : path.edge.reverse} pathLength={1}
                  style={{
                    animationDelay: `${path.delayMs}ms`, animationDuration: `${path.durationMs}ms`,
                    '--signal-strength': 0.84 * Math.pow(0.9, path.depth),
                  } as CSSProperties}
                />
              ))}
              {ambient.signal.litNodes.map(({ index, depth, delayMs }) => (
                <circle
                  key={index} className="login-graph-ambient-node"
                  data-node-index={index}
                  cx={nodes[index].x} cy={nodes[index].y} r={nodes[index].radius + 2}
                  style={{
                    animationDelay: `${delayMs}ms`, animationDuration: `${AMBIENT_NODE_FADE_MS}ms`,
                    '--signal-strength': 0.84 * Math.pow(0.9, depth),
                  } as CSSProperties}
                />
              ))}
            </g>
          )}
          {wave && (
            <g key={wave.id} className="login-graph-wave" data-origin={wave.origin} aria-hidden="true">
              <circle className="login-graph-origin-ring" data-node-index={wave.origin}
                cx={nodes[wave.origin].x} cy={nodes[wave.origin].y} r={nodes[wave.origin].radius + 7.5} />
              {edges.map((edge, index) => {
                const from = wave.depths[edge.from], to = wave.depths[edge.to]
                if (from < 0 || to < 0 || from === to || Math.max(from, to) > MAX_HOPS) return null
                return (
                  <path
                    key={index} className="login-graph-current"
                    data-edge-index={index} data-reverse={from >= to}
                    d={from < to ? edge.path : edge.reverse} pathLength={1}
                    data-depth={Math.min(from, to)}
                    style={{ animationDelay: `${Math.min(from, to) * STEP_MS}ms` }}
                  />
                )
              })}
              {nodes.map((node, index) => {
                const depth = wave.depths[index]
                if (depth < 0 || depth > MAX_HOPS) return null
                return (
                  <circle
                    key={index} className="login-graph-awaken"
                    data-node-index={index}
                    cx={node.x} cy={node.y} r={node.radius + 3.5}
                    style={{ animationDelay: `${depth * STEP_MS}ms` }}
                  />
                )
              })}
            </g>
          )}
          {nodes.map((node, index) => (
            <g
              key={index} className="login-graph-node" role="button"
              data-node-index={index}
              data-linked={highlightNode >= 0 && nodes[highlightNode].neighbors.includes(index) || undefined}
              tabIndex={focusNode === index ? 0 : -1}
              ref={element => { nodeRefs.current[index] = element }}
              aria-label={`记忆节点 ${index + 1}，传递电流`}
              onFocus={() => { setFocusNode(index); setKeyboardNode(index) }} onBlur={() => setKeyboardNode(-1)}
              onKeyDown={event => keyboard(event, index)}
            >
              <circle className="login-graph-base-dot" cx={node.x} cy={node.y} r={node.radius} fill={nodeColors[`${node.x},${node.y}`]} aria-hidden="true" />
              <circle className="login-graph-hit" cx={node.x} cy={node.y} r={24} />
              <circle className="login-graph-dot" cx={node.x} cy={node.y} r={node.radius + 1} style={{ animationDelay: `${index * -0.7}s` }} />
            </g>
          ))}
          </g>
        </g>
      </svg>
    </div>
  )
}
