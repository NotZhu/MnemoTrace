import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Maximize2, Minus, Plus, RotateCcw, SlidersHorizontal } from 'lucide-react'
import type { GraphData } from '../types'
import { copy as t } from '../lib/copy'
import { flushPreferences, usePreferences } from '../store/preferences'
import type { QuartzGraphController, QuartzGraphSettings } from '../vendor/quartz/types'
import { graphViewCache } from '../vendor/quartz/view'
import { visibleGraph } from '../lib/graphVisibility'

export function ForceGraph({ data, dataScope = '', liveOnly = false, selectedId = null, onSelect }: {
  data: GraphData
  dataScope?: string
  liveOnly?: boolean
  selectedId?: string | null
  onSelect?: (id: string | null) => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const controller = useRef<QuartzGraphController | null>(null)
  const settings = usePreferences((state) => state.graph)
  const viewScope = usePreferences((state) => state.activeScope)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const visibleData = visibleGraph(data, liveOnly)
  const hasVisibleNodes = visibleData.nodes.length > 0
  const appliedScope = useRef(dataScope)
  const current = useRef({ data, dataScope, liveOnly, selectedId, onSelect, settings })
  current.current = { data, dataScope, liveOnly, selectedId, onSelect, settings }
  const helpId = useId(), settingsId = useId()

  useEffect(() => {
    setReady(false); setError('')
    if (!viewScope) return
    const container = host.current!
    const abort = new AbortController()
    let instance: QuartzGraphController | null = null
    // 仅进入图谱页时加载 WebGL 与图谱依赖；详情选择不会重新创建画布或物理模拟。
    void import('../vendor/quartz/graph').then(async ({ createQuartzGraph }) => {
      if (abort.signal.aborted) return
      const initial = current.current
      instance = await createQuartzGraph(container, initial.data, {
        settings: initial.settings, selectedId: initial.selectedId, liveOnly: initial.liveOnly, signal: abort.signal,
        viewCache: graphViewCache(viewScope),
        onSelect: (id) => current.current.onSelect?.(id),
      })
      if (abort.signal.aborted) { instance.destroy(); return }
      controller.current = instance
      instance.resize(container.clientWidth, container.clientHeight)
      appliedScope.current = initial.dataScope
      if (initial.data !== current.current.data || initial.dataScope !== current.current.dataScope) {
        instance.setData(current.current.data, { preserveView: initial.dataScope === current.current.dataScope })
        appliedScope.current = current.current.dataScope
      }
      instance.setLiveOnly(current.current.liveOnly)
      instance.setSelected(current.current.selectedId)
      instance.configure(current.current.settings)
      setReady(true)
    }).catch((reason: unknown) => {
      if (!abort.signal.aborted) {
        instance?.destroy(); controller.current = null
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    })
    const observer = new ResizeObserver(([entry]) => {
      if (entry) instance?.resize(Math.round(entry.contentRect.width), Math.round(entry.contentRect.height))
    })
    observer.observe(container)
    return () => { abort.abort(); observer.disconnect(); instance?.destroy(); controller.current = null; flushPreferences() }
  }, [attempt, viewScope])

  useEffect(() => {
    if (!controller.current) return
    controller.current.setData(data, { preserveView: appliedScope.current === dataScope })
    appliedScope.current = dataScope
  }, [data, dataScope])
  useEffect(() => { controller.current?.setLiveOnly(liveOnly) }, [liveOnly])
  useEffect(() => { controller.current?.setSelected(selectedId) }, [selectedId])
  useEffect(() => { controller.current?.configure(settings) }, [settings])

  function change<K extends keyof QuartzGraphSettings>(key: K, value: QuartzGraphSettings[K]) {
    usePreferences.getState().setGraph({ [key]: value })
  }
  function reset() {
    usePreferences.getState().resetGraph()
    controller.current?.reset()
    onSelect?.(null)
  }
  function keyboard(event: KeyboardEvent<HTMLDivElement>) {
    const graph = controller.current
    if (!graph) return
    const step = event.shiftKey ? 90 : 30
    if (event.key === '+' || event.key === '=') graph.zoomBy(1.25)
    else if (event.key === '-') graph.zoomBy(1 / 1.25)
    else if (event.key === 'Home' || event.key === '0') graph.fit()
    else if (event.key === 'Escape') { onSelect?.(null); graph.setFocus(null); setSettingsOpen(false) }
    else if (event.key === 'ArrowLeft') graph.panBy(step, 0)
    else if (event.key === 'ArrowRight') graph.panBy(-step, 0)
    else if (event.key === 'ArrowUp') graph.panBy(0, step)
    else if (event.key === 'ArrowDown') graph.panBy(0, -step)
    else return
    event.preventDefault()
  }

  return <div className="graph-view">
    <div ref={host} className="graph-canvas" tabIndex={hasVisibleNodes ? 0 : -1} role="group" aria-label={t.graph.ariaLabel}
      aria-describedby={helpId} aria-busy={!ready && !error} onKeyDown={keyboard} />
    {!ready && !error && <div className="pointer-events-none absolute inset-0 grid place-items-center text-xs text-ghost" role="status">{t.graph.loading}</div>}
    {error && <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-8 text-center text-sm">
      <p className="text-sm text-foreground">{t.graph.renderError}</p>
      <p className="max-w-lg break-words text-xs text-ghost">{error}</p>
      <button type="button" className="rounded-lg border border-line px-4 py-2 text-foreground" onClick={() => setAttempt((value) => value + 1)}>{t.graph.retry}</button>
    </div>}
    <button type="button" className="graph-settings-toggle graph-tool" aria-label={t.graph.displaySettings}
      title={t.graph.displaySettings} aria-expanded={settingsOpen} aria-controls={settingsId}
      onClick={() => setSettingsOpen((value) => !value)}><SlidersHorizontal size={17} /></button>
    {settingsOpen && <section id={settingsId} className="graph-settings" aria-label={t.graph.displaySettings}>
      <div className="flex items-center justify-between">
        <h3>{t.graph.display}</h3>
        <button type="button" className="graph-tool" disabled={!ready} aria-label={t.graph.resetView} title={t.graph.resetView}
          onClick={reset}><RotateCcw size={15} /></button>
      </div>
      <label className="graph-setting-row"><span>{t.graph.nodeSize}</span><input type="range" min="0.6" max="1.8" step="0.1"
        value={settings.nodeScale} onChange={(event) => change('nodeScale', Number(event.target.value))} /></label>
      <label className="graph-setting-row"><span>{t.graph.lineThickness}</span><input type="range" min="0.4" max="1.6" step="0.1"
        value={settings.lineWidth} onChange={(event) => change('lineWidth', Number(event.target.value))} /></label>
      <label className="graph-setting-row"><span>{t.graph.labelVisibility}</span><input type="range" min="1" max="6" step="0.25"
        value={settings.opacityScale} onChange={(event) => change('opacityScale', Number(event.target.value))} /></label>
      <button type="button" role="switch" aria-checked={settings.showRelations} className="graph-setting-switch"
        onClick={() => change('showRelations', !settings.showRelations)}>
        <span aria-hidden="true" className="switch-track"><span className="switch-thumb" /></span>
        <span>{t.graph.showRelations}</span>
      </button>
      <h3 className="graph-settings-divider">{t.graph.forces}</h3>
      <label className="graph-setting-row"><span>{t.graph.repulsion}</span><input type="range" min="0.1" max="2.5" step="0.1"
        value={settings.repelForce} onChange={(event) => change('repelForce', Number(event.target.value))} /></label>
      <label className="graph-setting-row"><span>{t.graph.centerForce}</span><input type="range" min="0" max="1" step="0.05"
        value={settings.centerForce} onChange={(event) => change('centerForce', Number(event.target.value))} /></label>
      <label className="graph-setting-row"><span>{t.graph.linkDistance}</span><input type="range" min="10" max="160" step="5"
        value={settings.linkDistance} onChange={(event) => change('linkDistance', Number(event.target.value))} /></label>
      <label className="graph-setting-row"><span>{t.graph.linkForce}</span><input type="range" min="0.1" max="1" step="0.05"
        value={settings.linkForce} onChange={(event) => change('linkForce', Number(event.target.value))} /></label>
    </section>}
    <div className="graph-toolbar" role="toolbar" aria-label={t.graph.viewControls}>
      <button type="button" className="graph-tool" disabled={!ready || !hasVisibleNodes} aria-label={t.graph.zoomOut} title={t.graph.zoomOut}
        onClick={() => controller.current?.zoomBy(1 / 1.25)}><Minus size={16} /></button>
      <button type="button" className="graph-tool" disabled={!ready || !hasVisibleNodes} aria-label={t.graph.zoomIn} title={t.graph.zoomIn}
        onClick={() => controller.current?.zoomBy(1.25)}><Plus size={16} /></button>
      <button type="button" className="graph-tool" disabled={!ready || !hasVisibleNodes} aria-label={t.graph.fitView} title={t.graph.fitView}
        onClick={() => controller.current?.fit()}><Maximize2 size={16} /></button>
    </div>
    <ul className="sr-only" aria-label={t.graph.entities}>
      {visibleData.nodes.map((node) => <li key={node.id}><button type="button" aria-pressed={node.id === selectedId}
        onFocus={() => controller.current?.setFocus(node.id)} onBlur={() => controller.current?.setFocus(null)}
        onClick={() => onSelect?.(node.id === selectedId ? null : node.id)}>{node.name}</button></li>)}
    </ul>
    <p id={helpId} className="sr-only">{t.graph.gestureHint} {t.graph.keyboardHint}</p>
  </div>
}
