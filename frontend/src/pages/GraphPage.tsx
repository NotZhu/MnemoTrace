

import { useEffect, useRef, useState } from 'react'
import { MousePointer2, Search, Share2, X } from 'lucide-react'

import { Card, EmptyState, ErrorState, PageHeader, Spinner } from '../components/ui'
import { ForceGraph } from '../components/ForceGraph'
import { useGraph } from '../hooks/queries'
import { useDebouncedSearch } from '../hooks/useDebouncedSearch'
import { useSpacePreference } from '../store/preferences'
import { copy as t } from '../lib/copy'
import { visibleGraph } from '../lib/graphVisibility'
import type { GraphData } from '../types'

const emptyGraph: GraphData = { nodes: [], edges: [] }

export default function GraphPage() {
  const [q, setQ] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const [composing, setComposing] = useState(false)
  const searchQuery = useDebouncedSearch(q, composing, 500)
  const [liveOnly, setLiveOnly] = useSpacePreference('graph', 'liveOnly')
  const [hideSensitive, setHideSensitive] = useSpacePreference('graph', 'hideSensitive')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const { data, isLoading, isFetching, isPlaceholderData, isError, error, refetch } = useGraph({
    q: searchQuery,
    live_only: liveOnly,
    include_sensitive: !hideSensitive,
    limit: 120,
  }, { keepPreviousData: true })

  const visibleData = data ? visibleGraph(data, liveOnly) : undefined
  const hasGraph = (visibleData?.nodes.length ?? 0) > 0
  const pending = isLoading || isFetching || isPlaceholderData
  const settled = data && !pending && !isError
  const filteredEmpty = Boolean(searchQuery.trim() || hideSensitive || liveOnly)
  const selected = visibleData?.nodes.find((n) => n.id === selectedId) ?? null
  const related = selected
    ? (visibleData?.edges ?? []).filter((e) => e.source === selected.id || e.target === selected.id)
    : []
  useEffect(() => {
    if (!data || isError || !selectedId || selected) return
    // 截断旧图的局部空结果不能证明所选实体在新查询中不存在。
    if (settled || (isPlaceholderData && liveOnly && data.has_more === false)) setSelectedId(null)
  }, [data, selectedId, selected, isError, settled, isPlaceholderData, liveOnly])

  return (
    <div className="graph-page">
      <PageHeader title={t.graph.title} />

      <Card className="shrink-0 !p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-xl border border-line bg-ink-700 px-3">
            <Search className="h-4 w-4 shrink-0 text-ghost" />
            <input
              ref={searchInput}
              aria-label={t.graph.searchPlaceholder}
              className="min-w-0 w-full bg-transparent py-2 text-sm outline-none placeholder:text-ghost/70"
              placeholder={t.graph.searchPlaceholder}
              value={q}
              onCompositionStart={() => setComposing(true)}
              onCompositionEnd={() => setComposing(false)}
              onChange={(e) => {
                setQ(e.target.value)
                setSelectedId(null)
              }}
            />
            {q.length > 0 && (
              <button
                type="button"
                aria-label={t.graph.clearSearch}
                onClick={() => {
                  setQ('')
                  setComposing(false)
                  setSelectedId(null)
                  searchInput.current?.focus()
                }}
                className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-ghost transition hover:bg-selected hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <button type="button" role="switch" aria-checked={liveOnly} aria-label={t.graph.currentOnly} onClick={() => setLiveOnly((value) => !value)} className="inline-flex min-h-[38px] select-none items-center gap-2.5 rounded-full px-1 text-sm text-ghost">
            <span aria-hidden="true" className="switch-track"><span className="switch-thumb" /></span>
            {t.graph.currentOnly}
          </button>
          <button type="button" role="switch" aria-checked={hideSensitive} aria-label={t.graph.hideSensitive} onClick={() => setHideSensitive((value) => !value)} className="inline-flex min-h-[38px] select-none items-center gap-2.5 rounded-full px-1 text-sm text-ghost">
            <span aria-hidden="true" className="switch-track"><span className="switch-thumb" /></span>
            {t.graph.hideSensitive}
          </button>
        </div>
      </Card>

      <div className="graph-layout">
        <div className="h-full min-h-0 min-w-0">
          <section className="graph-workspace" aria-busy={Boolean(pending)}>
            <div className="graph-status">
              <h3 className={isError && !pending ? 'text-sm' : undefined}>{pending ? t.graph.updating : isError ? t.graph.renderError : t.graph.stats(visibleData?.nodes.length ?? 0, visibleData?.edges.length ?? 0)}</h3>
              {settled && data.has_more && <p className="mt-1 max-w-sm" role="status">{t.graph.truncated(visibleData?.edges.length ?? 0)}</p>}
              {hasGraph && <span className="mt-1.5 flex items-center gap-3 text-[10px] text-ghost">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-px w-4 bg-ghost/65" /> {t.graph.legendLive}
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-px w-4 border-t border-dashed border-ghost/65" /> {t.graph.legendOld}
                    </span>
                  </span>}
            </div>

            <ForceGraph data={data ?? emptyGraph} dataScope={JSON.stringify([searchQuery, hideSensitive])}
              liveOnly={liveOnly} selectedId={selected?.id ?? null} onSelect={setSelectedId} />
            {pending && !hasGraph && <div className="pointer-events-none absolute inset-0 grid place-items-center"><Spinner label={t.graph.loading} /></div>}
            {isError && <div className="absolute inset-x-4 top-16 z-20"><ErrorState message={(error as Error).message} onRetry={() => void refetch()} /></div>}
            {settled && !hasGraph && !data.has_more && <div className="graph-empty-state" role="status">
              <EmptyState title={filteredEmpty ? t.graph.noResultsTitle : t.graph.emptyTitle}
                hint={filteredEmpty ? t.graph.noResultsHint : t.graph.emptyHint} icon={<Share2 className="h-6 w-6" />} />
            </div>}
          </section>
        </div>
        <aside className="graph-inspector" aria-label={t.graph.entityInfo}>
          <div className="mb-4 flex min-h-8 shrink-0 items-center justify-between gap-2">
            <h3 className="text-sm font-medium text-foreground">{t.graph.entityInfo}</h3>
            {selected && <button type="button" aria-label={t.graph.closeDetails} title={t.graph.closeDetails}
              className="graph-tool shrink-0" onClick={() => setSelectedId(null)}><X size={15} /></button>}
          </div>
          {selected ? (
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
              <div>
                <div className="text-xs uppercase tracking-wide text-ghost">{selected.type}</div>
                <div className="mt-1 break-words text-sm font-semibold text-slate-100">{selected.name}</div>
              </div>
              <div className="text-xs text-ghost">{t.graph.edgeCount(related.length)}</div>
              <ul className="space-y-2">
                {related.slice(0, 12).map((e, i) => {
                  const sourceName = data?.nodes.find((n) => n.id === e.source)?.name ?? e.source
                  const targetName = data?.nodes.find((n) => n.id === e.target)?.name ?? e.target
                  const factText = e.fact_text?.trim()
                  const repeatsRelation = factText?.replace(/\s+/g, ' ') === `${sourceName} ${e.predicate} ${targetName}`
                  return (
                    <li key={`${e.fact_id ?? i}`} className="rounded-lg bg-ink-700/70 p-2 text-xs leading-relaxed">
                      <div className={`break-words text-slate-200${e.live ? '' : ' line-through'}`}>
                        {e.source === selected.id ? <strong className="font-semibold text-foreground">{sourceName}</strong> : <span>{sourceName}</span>}
                        {' '}<span className="text-brand-cyan">{e.predicate}</span>{' '}
                        {e.target === selected.id ? <strong className="font-semibold text-foreground">{targetName}</strong> : <span>{targetName}</span>}
                      </div>
                      {factText && !repeatsRelation && <div className={`mt-1 break-words text-ghost${e.live ? '' : ' line-through'}`}>{factText}</div>}
                      <div className="mt-1 text-[11px] text-ghost">
                        {e.valid_at_h}
                        {!e.live && e.invalid_at_h ? ` · ${t.graph.legendOld} ${e.invalid_at_h}` : ''}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : (
            <div className="graph-inspector-empty flex min-h-0 flex-1 flex-col overflow-y-auto px-2 py-4 text-center">
              <div className="my-auto flex shrink-0 flex-col items-center gap-3">
                <div className="grid h-14 w-14 place-items-center rounded-2xl border border-field-line bg-icon-surface text-brand-cyan">
                  <MousePointer2 aria-hidden="true" className="h-6 w-6" strokeWidth={1.5} />
                </div>
                <p className="max-w-52 text-xs leading-relaxed text-ghost">{t.graph.nodeHint}</p>
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
