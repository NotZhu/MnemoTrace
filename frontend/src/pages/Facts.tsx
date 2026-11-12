

import { useRef, useState } from 'react'
import { FileSearch, GitMerge, Loader2, Search, ShieldAlert, Trash2, X } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { FilterSelect } from '../components/FilterSelect'
import { CategoryBadges, CategoryFilter } from '../components/CategoryEditor'
import { ConfirmDialog, Modal } from '../components/Modal'
import { toast } from '../components/Toast'
import {
  useConflicts,
  useDeleteFact,
  useSetFactSensitive,
  useMemoryPages,
  useSourceEpisodes,
  useResolveConflict,
} from '../hooks/queries'
import { copy as t } from '../lib/copy'
import { splitStamp } from '../lib/format'
import { useSpacePreference } from '../store/preferences'
import type { MemoryEpisode, MemoryFact } from '../types'

export default function Facts() {
  const sensitivity = useSetFactSensitive()
  const deleteFact = useDeleteFact()

  const [q, setQ] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const [status, setStatus] = useSpacePreference('facts', 'status')
  const [categories, setCategories] = useSpacePreference('facts', 'categories')
  const [hideSensitive, setHideSensitive] = useSpacePreference('facts', 'hideSensitive')
  const [sourceOf, setSourceOf] = useState<MemoryFact | null>(null)
  const [confirmFact, setConfirmFact] = useState<MemoryFact | null>(null)

  const { data, isFetching, isError, isSuccess, isPlaceholderData, error, refetch, loadMore } = useMemoryPages({
    // 只取属性类事实 —— 会话结论有自己的页面；混在一起的话，更新、更庞大的那类
    // 会在分页和列表里把另一类完全淹没。
    kind: 'attributes',
    facts_limit: 40,
    episodes_limit: 0,
    q,
    status: status === 'all' ? undefined : status,
    categories: categories.length ? categories : undefined,
    include_sensitive: !hideSensitive,
  })
  const facts = data?.facts ?? []
  const sourceQuery = useSourceEpisodes(!!sourceOf)
  const factsPage = data?.facts_page
  const hasMore = factsPage?.has_more ?? false
  const hasFilters = Boolean(q.trim() || categories.length || status !== 'live')
  const clearFilters = () => {
    setQ('')
    setCategories([])
    setStatus('live')
    searchInput.current?.focus()
  }
  const resetControl = hasFilters ? (
    <button type="button" onClick={clearFilters} aria-label={t.facts.resetFilters}
      className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-control-line/60 bg-transparent px-3 py-2 text-sm font-semibold text-brand-cyan transition-colors hover:bg-action-hover hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan">
      <X aria-hidden="true" className="h-4 w-4" /> {t.facts.resetFilters}
    </button>
  ) : null

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6">
      <PageHeader title={t.facts.title} />
      {isError && <ErrorState message={(error as Error).message} isRetrying={isFetching} onRetry={() => { void refetch() }} />}

      <ConflictsCard />

      <div className="flex min-h-0 flex-1 flex-col gap-3">
        <div className="grid items-center gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
          <div aria-busy={isFetching} className="flex h-14 min-w-0 items-center gap-2 rounded-2xl border border-line bg-ink-700 pl-4 pr-2">
            {isFetching
              ? <Loader2 className="h-4 w-4 shrink-0 animate-spin text-ghost motion-reduce:animate-none" />
              : <Search className="h-4 w-4 shrink-0 text-ghost" />}
            <input
              ref={searchInput}
              aria-label={t.facts.searchPlaceholder}
              className="h-full min-w-0 w-full bg-transparent py-3 text-base outline-none placeholder:text-ghost/70"
              placeholder={t.facts.searchPlaceholder}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            {q.length > 0 && (
              <button
                type="button"
                aria-label={t.facts.clearSearch}
                title={t.facts.clearSearch}
                onClick={() => { setQ(''); searchInput.current?.focus() }}
                className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-ghost transition hover:bg-selected hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 pt-1">
          <p className="text-xs text-ghost" aria-live="polite">{!data || isPlaceholderData || isError ? t.facts.countPending : t.facts.factCount(factsPage?.total ?? facts.length)}</p>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-x-6 gap-y-3 pr-3">
            <CategoryFilter value={categories} onChange={setCategories} />
            <FilterSelect label={t.facts.statusFilter} value={status} onChange={(value) => setStatus(value as typeof status)}
              options={[
                { value: 'live', label: t.facts.currentOnly },
                { value: 'superseded', label: t.facts.historyOnly },
                { value: 'all', label: t.facts.allStatuses },
              ]} />
            <button
              type="button"
              role="switch"
              aria-checked={hideSensitive}
              aria-label={t.facts.hideSensitive}
              onClick={() => setHideSensitive((value) => !value)}
              className="inline-flex min-h-[38px] select-none items-center gap-2.5 rounded-full text-sm text-ghost focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
            >
              <span aria-hidden="true" className="switch-track">
                <span className="switch-thumb" />
              </span>
              {t.facts.hideSensitive}
            </button>
          </div>
        </div>
      <div className="flex flex-1 flex-col" aria-busy={isFetching}>
        {!data ? (isError ? null : <Spinner label={t.facts.loading} />) : facts.length ? (
          <>
          {isPlaceholderData && <p role="status" className="px-4 py-2 text-xs text-ghost">{t.facts.updating}</p>}
          <ul className="divide-y divide-line">
            {facts.map((f) => {
              const { date, time } = splitStamp(f.valid_at)
              return (
              <li key={f.id} className="group flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:gap-5 sm:px-5">
                <div className="min-w-0 flex-1 break-words">
                  <div className={f.status === 'live' ? 'text-sm leading-relaxed text-slate-100' : 'text-sm leading-relaxed text-ghost line-through'}>
                    {f.text}
                  </div>
                  <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-ghost">
                    <span className={`inline-flex items-center gap-1.5 ${f.status === 'live' ? 'font-medium text-brand-mint' : 'text-ghost'}`}>
                      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
                      {f.withdrawn_at != null ? t.facts.statusWithdrawn : f.status === 'live' ? t.facts.statusLive : t.facts.statusOld}
                    </span>
                    <time className="tabular-nums">{date}{time && ` · ${time}`}</time>
                    {f.sensitive && (
                      <span className="inline-flex items-center gap-1 rounded bg-brand-rose/15 px-1.5 py-px text-brand-rose">
                        <ShieldAlert className="h-3 w-3" /> {t.facts.sensitive}
                      </span>
                    )}
                    {f.source === 'user' && (
                      <span className="fact-blue-tag" title={t.facts.manualHint}>
                        {t.facts.manualLabel}
                      </span>
                    )}
                    <CategoryBadges categories={f.categories} categories_source={f.categories_source} />
                    {f.invalid_at && <span>{t.facts.invalidAt(f.invalid_at)}</span>}
                  </div>
                </div>
                <div className="flex shrink-0 items-center justify-end gap-1">
                  {!!f.provenance?.length && (
                    <button
                      onClick={() => setSourceOf(f)}
                      className="grid h-8 w-8 place-items-center rounded-lg text-ghost transition hover:bg-selected hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
                      aria-label={t.facts.viewSource}
                      title={t.facts.viewSource}
                    >
                      <FileSearch className="h-4 w-4" />
                    </button>
                  )}
                  <button
                    onClick={() => sensitivity.mutate({ id: f.id, sensitive: !f.sensitive }, {
                      onError: (e) => toast.error(e.message),
                    })}
                    disabled={sensitivity.isPending}
                    className="grid h-8 w-8 place-items-center rounded-lg text-ghost transition hover:bg-selected hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
                    aria-label={f.sensitive ? "取消敏感标记" : "标为敏感"}
                    title={f.sensitive ? "取消敏感标记" : "标为敏感"}
                  >
                    <ShieldAlert className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => setConfirmFact(f)}
                    className="grid h-8 w-8 place-items-center rounded-lg text-ghost transition hover:bg-selected hover:text-brand-rose focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
                    aria-label={t.common.delete}
                    title={t.common.delete}
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </li>
              )
            })}
          </ul>
          {hasMore && (
            <div className="flex justify-center border-t border-line py-3">
              <Button variant="ghost" disabled={isPlaceholderData || isFetching || isError} onClick={() => { void loadMore() }}>
                {t.facts.loadMore}
              </Button>
            </div>
          )}
          </>
        ) : isPlaceholderData || !isSuccess ? <Spinner label={t.facts.updating} /> : (
          <PageEmptyState title={t.facts.emptyTitle} hint={hasFilters ? t.facts.noResultsHint : t.facts.emptyHint} footer={resetControl} />
        )}
      </div>
      </div>

      {/* 溯源（provenance）：无法追溯来源的事实就无从谈信任 */}
      <Modal open={!!sourceOf} title={t.facts.sourceTitle} onClose={() => setSourceOf(null)}>
        {sourceOf && (
          <div className="space-y-4">
            {sourceQuery.isError ? (
              <ErrorState message={`来源读取失败：${(sourceQuery.error as Error).message}`} onRetry={() => { void sourceQuery.refetch() }} />
            ) : sourceQuery.isLoading ? (
              <Spinner label={t.facts.sourceLoading} />
            ) : (
              (() => {
                const eps = (sourceQuery.data?.episodes ?? []).filter((e: MemoryEpisode) =>
                  sourceOf.provenance?.includes(e.id),
                )
                if (!eps.length) return <p className="text-sm text-ghost">{t.facts.sourceMissing}</p>
                return (
                  <div className="space-y-3">
                    <p className="text-sm text-ghost">{t.facts.sourceFrom(eps.length)}</p>
                    {eps.map((e: MemoryEpisode) => (
                      <div key={e.id} className="rounded-[20px] border border-line bg-ink-900/50 p-4">
                        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-ghost">
                          <span className="shrink-0 text-brand-cyan">{e.date}</span>
                          <div className="ml-auto flex min-w-0 flex-wrap justify-end gap-2">
                          <span aria-label={`${t.facts.sessionLabel}：${e.session}`} className="rounded-md bg-ink-700/60 px-2 py-0.5 font-mono text-xs">{e.session}</span>
                          <span title={`${t.facts.relationLabel}：${sourceOf.predicate}`} aria-label={`${t.facts.relationLabel}：${sourceOf.predicate}`} className="rounded-md bg-ink-700/60 px-2 py-0.5 font-mono text-xs">{sourceOf.predicate}</span>
                          </div>
                        </div>
                        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-100">
                          {e.content}
                        </p>
                      </div>
                    ))}
                  </div>
                )
              })()
            )}
          </div>
        )}
      </Modal>

      {/* 删除 */}
      <ConfirmDialog
        open={!!confirmFact}
        title={t.facts.deleteTitle}
        danger
        confirmLabel={t.common.delete}
        loading={deleteFact.isPending}
        body={
          <div className="space-y-3">
            <p>{t.facts.deleteBody}</p>
            {confirmFact && (
              <p className="rounded-lg border border-brand-rose/25 bg-brand-rose/10 px-3 py-2 text-brand-rose">
                {t.facts.deleteTarget(confirmFact.text)}
              </p>
            )}
          </div>
        }
        onClose={() => setConfirmFact(null)}
        onConfirm={() => {
          if (!confirmFact) return
          deleteFact.mutate(confirmFact.id, {
            onSuccess: () => {
              toast.success(t.facts.deleted)
              setConfirmFact(null)
            },
            onError: (e) => toast.error(String((e as Error).message)),
          })
        }}
      />
    </div>
  )
}

function ConflictsCard() {
  const { data } = useConflicts()
  const resolve = useResolveConflict()
  const items = data?.conflicts ?? []
  if (!items.length) return null

  const act = (id: string, keep: 'newer' | 'older' | 'both', label: string) =>
    resolve.mutate(
      { id, keep },
      { onSuccess: () => toast.success(label), onError: (e) => toast.error(String((e as Error).message)) },
    )

  return (
    <Card className="!border-brand-amber/30 !bg-brand-amber/[0.06]">
      <CardTitle hint={t.facts.conflictsHint}>
        <span className="inline-flex items-center gap-2 text-brand-amber">
          <GitMerge className="h-4 w-4" /> {t.facts.conflictsTitle(items.length)}
        </span>
      </CardTitle>
      <ul className="space-y-3">
        {items.map((c) => (
          <li key={c.id} className="rounded-xl border border-line bg-ink-700/40 p-3">
            <div className="grid gap-1.5 sm:grid-cols-2">
              <div className="rounded-lg bg-brand-mint/10 px-2.5 py-1.5 text-sm">
                <span className="text-[10px] text-brand-mint">{t.facts.conflictNew}</span>
                <div className="text-slate-100">{c.text_newer}</div>
              </div>
              <div className="rounded-lg bg-ink-700/60 px-2.5 py-1.5 text-sm">
                <span className="text-[10px] text-ghost">{t.facts.conflictOld}</span>
                <div className="text-ghost line-through">{c.text_older}</div>
              </div>
            </div>
            <div className="mt-2.5 flex flex-wrap gap-2">
              <Button onClick={() => act(c.id, 'newer', t.facts.keepNewerToast)}>{t.facts.keepNewer}</Button>
              <Button variant="ghost" onClick={() => act(c.id, 'older', t.facts.keepOlderToast)}>
                {t.facts.keepOlder}
              </Button>
              <Button variant="ghost" onClick={() => act(c.id, 'both', t.facts.keepBothToast)}>
                {t.facts.keepBoth}
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </Card>
  )
}
