

import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { Check, ChevronRight, HelpCircle, Loader2, NotebookText, Pencil, Search, Trash2, X } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { ConfirmDialog } from '../components/Modal'
import { toast } from '../components/Toast'
import { useCloseSession, useDeleteFact, useEditFact, useMemories, useSessions } from '../hooks/queries'
import { useDebouncedSearch } from '../hooks/useDebouncedSearch'
import { useSpacePreference } from '../store/preferences'
import { copy as t } from '../lib/copy'
import { splitStamp } from '../lib/format'
import type { MemoryFact } from '../types'

/** 会话结论的档案。
 *
 * 逐轮抽取产出属性；一个会话产出结论。两者都是普通的 Fact —— 结论只是 predicate 恰好属于
 * 四种结果类型之一的事实 —— 所以这个页面是对同一存储的另一种*读法*，而不是第二个存储。
 * 这里的单位是会话，因为一条结论依赖的是整段对话，而不是其中某一行。
 */

const KIND_ORDER = ['decision', 'finding', 'lesson', 'open_question'] as const
export type OutcomeKind = (typeof KIND_ORDER)[number]

const KIND_COLOR: Record<string, string> = {
  decision: 'text-brand-cyan',
  finding: 'text-brand-mint',
  lesson: 'text-brand-amber',
  open_question: 'text-brand-violet',
}

const OUTCOME_TEXT_CLASS = 'text-[15px] leading-relaxed whitespace-pre-wrap break-words'

const kindRank = (predicate: string) => {
  const i = KIND_ORDER.indexOf(predicate.toLowerCase() as OutcomeKind)
  return i === -1 ? KIND_ORDER.length : i
}

export interface OutcomeGroup {
  session: string
  facts: MemoryFact[]
}

/** 把结论按其所属会话分组（结论的 subject 就是会话 id）。
 *
 * 分组连续进行，并保留服务端顺序（valid_at 倒序）：重新排序会在每次加载下一页时
 * 打乱整个档案。 */
export function groupOutcomes(facts: MemoryFact[]): OutcomeGroup[] {
  const groups: OutcomeGroup[] = []
  for (const f of facts) {
    const last = groups[groups.length - 1]
    if (last && last.session === f.subject) last.facts.push(f)
    else groups.push({ session: f.subject, facts: [f] })
  }
  for (const g of groups) g.facts.sort((a, b) => kindRank(a.predicate) - kindRank(b.predicate))
  return groups
}

export default function Journal() {
  const [q, setQ] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const [composing, setComposing] = useState(false)
  const searchQuery = useDebouncedSearch(q, composing)
  const [showSuperseded, setShowSuperseded] = useSpacePreference('journal', 'showSuperseded')
  const [kindChip, setKindChip] = useState<OutcomeKind | 'all'>('all')
  const [limit, setLimit] = useState(60)

  useEffect(() => {
    setLimit(60)
  }, [q, showSuperseded, kindChip])

  const { data, isLoading, isFetching, isError, error, refetch } = useMemories({
    kind: 'outcomes',
    facts_limit: limit,
    episodes_limit: 0,
    q: searchQuery,
    status: showSuperseded ? undefined : 'live',
    include_sensitive: true,
  }, { keepPreviousData: true })

  const facts = data?.facts ?? []
  const rows = kindChip === 'all' ? facts : facts.filter((f) => f.predicate.toLowerCase() === kindChip)
  const groups = groupOutcomes(rows)
  const hasMore = data?.facts_page?.has_more ?? false
  const open = facts.filter((f) => f.status === 'live' && f.predicate.toLowerCase() === 'open_question').slice(0, 8)
  const hasSearch = Boolean(q.trim())
  const hasKindFilter = kindChip !== 'all'
  const hasFilters = hasSearch || hasKindFilter
  const searchUpdating = composing || q !== searchQuery || isFetching

  const clearFilters = () => {
    setQ('')
    setComposing(false)
    setKindChip('all')
    searchInput.current?.focus()
  }
  const resetControl = hasFilters ? (
    <button type="button" onClick={clearFilters} aria-label={t.journal.resetFilters}
      className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-control-line/60 bg-transparent px-3 py-2 text-sm font-semibold text-brand-cyan transition-colors hover:bg-action-hover hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan">
      <X aria-hidden="true" className="h-4 w-4" /> {t.journal.resetFilters}
    </button>
  ) : null
  const resetInEmptyState = !isLoading && !isError && !rows.length && !searchUpdating && hasFilters

  let lastDate = ''

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6">
      <PageHeader title={t.journal.title} />

      {open.length > 0 && <OpenQuestionsCard items={open} />}

      <div className="space-y-4">
        <div aria-busy={searchUpdating} className="flex w-full items-center gap-2 rounded-2xl border border-line bg-ink-700 px-3">
          {searchUpdating
            ? <Loader2 className="h-4 w-4 shrink-0 animate-spin text-ghost motion-reduce:animate-none" />
            : <Search className="h-4 w-4 shrink-0 text-ghost" />}
          <input
            ref={searchInput}
            aria-label={t.journal.searchPlaceholder}
            className="min-w-0 w-full bg-transparent py-3 text-sm outline-none placeholder:text-ghost/70"
            placeholder={t.journal.searchPlaceholder}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={(e) => { setComposing(false); setQ(e.currentTarget.value) }}
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div role="group" aria-label={t.journal.kindFilterLabel} className="grid max-w-full grid-cols-5 gap-1 rounded-xl border border-line/50 bg-ink-700/70 p-1">
            <KindChip active={kindChip === 'all'} onClick={() => setKindChip('all')} label={t.journal.kindAll} />
            {KIND_ORDER.map((k) => (
              <KindChip
                key={k}
                active={kindChip === k}
                onClick={() => setKindChip(k)}
                label={t.journal.kind[k]}
              />
            ))}
          </div>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-3">
            <button
              type="button"
              role="switch"
              aria-checked={showSuperseded}
              aria-label={t.journal.showSuperseded}
              onClick={() => setShowSuperseded((value) => !value)}
              className="inline-flex min-h-[38px] shrink-0 select-none items-center gap-2.5 rounded-full px-1 text-sm text-ghost"
            >
              <span aria-hidden="true" className="switch-track">
                <span className="switch-thumb" />
              </span>
              <span>{t.journal.showSuperseded}</span>
            </button>
          </div>
        </div>
      </div>

      {isError && <ErrorState message={`${data ? '结论更新失败，显示上次结果并保留编辑：' : ''}${(error as Error).message}`} onRetry={() => { void refetch() }} />}
      {isLoading && !data ? <Spinner label={t.journal.loading} /> : isError && !data ? null : rows.length ? (
        <>
          <div>
            {/* 行始终在同一父级按事实 ID 协调；分组只控制外观，不能决定编辑器寿命。 */}
            {groups.flatMap((g) => {
              const { date } = splitStamp(g.facts[0]?.valid_at)
              const newDay = date !== lastDate
              lastDate = date
              return g.facts.map((fact, index) => {
                const first = index === 0
                const last = index === g.facts.length - 1
                return (
                  <Fragment key={fact.id}>
                    {first && newDay && (
                      <h2 className="sticky top-0 z-10 -mx-1 mb-3 mt-4 bg-ink-900/85 px-1 py-1.5 text-xs font-semibold tabular-nums tracking-wide text-brand-cyan backdrop-blur first:mt-0">
                        {date}
                      </h2>
                    )}
                    <div className={`border-x border-line bg-ink-800 px-5 sm:px-6 ${first ? `rounded-t-[20px] border-t pt-5 sm:pt-6 ${newDay ? '' : 'mt-4 first:mt-0'}` : ''} ${last ? 'rounded-b-[20px] border-b pb-5 sm:pb-6' : ''}`}>
                      {first && <OutcomeGroupHeading group={g} />}
                      <ul className={`${first ? '' : 'border-t border-line/50 [&>li]:!pt-3'} ${last ? '' : '[&>li]:!pb-3'}`}>
                        <OutcomeRow fact={fact} editable />
                      </ul>
                    </div>
                  </Fragment>
                )
              })
            })}
          </div>
          {hasMore && (
            <div className="flex justify-center">
              <Button variant="ghost" onClick={() => setLimit((n) => n + 60)}>
                {t.journal.loadMore}
              </Button>
            </div>
          )}
        </>
      ) : searchUpdating ? (
        <Spinner label={t.journal.searching} />
      ) : hasFilters ? (
        <PageEmptyState title={t.journal.noResultsTitle} hint={t.journal.noResultsHint} icon={<Search className="h-6 w-6" />} footer={resetControl} />
      ) : (
        <JournalEmpty />
      )}

      {/* 有意不在只有空状态时才显示：蒸馏过一个会话不代表积压已清完，而且一旦出现结论就
          隐藏这个入口，其余的历史记录在这页就没有任何进入路径了。重复蒸馏已蒸馏过的
          会话是无害的空操作。 */}
      {rows.length > 0 && (
        <Card>
          <BackfillSessions />
        </Card>
      )}
      {hasFilters && !resetInEmptyState && <div className="mt-auto flex justify-center pt-6" data-filter-reset-footer>{resetControl}</div>}
    </div>
  )
}

function KindChip({
  active,
  label,
  onClick,
}: {
  active: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`whitespace-nowrap rounded-lg px-3 py-2 text-sm font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan sm:px-4 ${active ? 'bg-action text-foreground shadow-sm' : 'text-ghost hover:bg-ink-700 hover:text-foreground'}`}
    >
      {label}
    </button>
  )
}

function OpenQuestionsCard({ items }: { items: MemoryFact[] }) {
  return (
    <Card className="!border-brand-amber/30 !bg-brand-amber/[0.06]">
      <CardTitle>
        <span className="inline-flex items-center gap-2 text-brand-amber">
          <HelpCircle className="h-4 w-4" /> {t.journal.openQuestionsTitle(items.length)}
        </span>
      </CardTitle>
      <ul className="space-y-2">
        {items.map((f) => {
          const { date } = splitStamp(f.valid_at)
          return (
            <li key={f.id} className="flex items-baseline gap-2.5 text-sm">
              <time className="shrink-0 text-[11px] tabular-nums text-ghost">{date}</time>
              <span className="min-w-0 break-words leading-relaxed text-slate-100">{f.display || f.text}</span>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

/** 单个会话的结论。除非传入 `editable` 否则只读 —— 修正应该在 Journal 页做，一个能顺手
 *  改写记忆的 Dashboard 预览是错误的交互设计。 */
export function SessionOutcomes({
  group,
  editable = false,
  className,
}: {
  group: OutcomeGroup
  editable?: boolean
  className?: string
}) {
  return (
    <Card className={className}>
      <OutcomeGroupHeading group={group} />
      <ul className="divide-y divide-line/50">
        {group.facts.map((f) => <OutcomeRow key={f.id} fact={f} editable={editable} />)}
      </ul>
    </Card>
  )
}

function OutcomeGroupHeading({ group }: { group: OutcomeGroup }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-[13px] text-ghost">
      <span className="min-w-0 max-w-full truncate">{t.journal.sessionLabel} {group.session}</span>
      <span>{t.journal.conclusionCount(group.facts.length)}</span>
    </div>
  )
}

function OutcomeRow({
  fact,
  editable,
}: {
  fact: MemoryFact
  editable: boolean
}) {
  const editFact = useEditFact()
  const deleteFact = useDeleteFact()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [mine, setMine] = useState(false)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const evidenceId = useId()
  const saveInFlight = useRef(false)

  const kind = fact.predicate.toLowerCase()
  const statement = fact.display || fact.text
  const isMine = mine || fact.source === 'user'
  const manualMarker = isMine ? (
    <span className="text-xs leading-relaxed text-ghost" title={t.journal.mineHint}>
      {t.journal.mine}
    </span>
  ) : null

  const start = () => {
    if (!editable) return
    setDraft(statement)
    setEditing(true)
  }

  const save = () => {
    if (saveInFlight.current || editFact.isPending) return
    const next = draft.trim()
    if (!next) return
    if (next === statement) {
      setEditing(false)
      return
    }
    saveInFlight.current = true
    editFact.mutate(
      { id: fact.id, patch: { object: next } },
      {
        onSuccess: (r) => {
          // 编辑会把该事实标记为用户亲笔 —— 用户由此知道这条修正能在下次蒸馏中幸存，
          // 而不是被悄悄地重新抽取覆盖。
          setMine(r.source === 'user')
          toast.success(t.journal.saved)
          setEditing(false)
        },
        onError: (e) => toast.error(String((e as Error).message)),
        onSettled: () => { saveInFlight.current = false },
      },
    )
  }

  return (
    <li className="group flex items-start gap-3 py-3 first:pt-0 last:pb-0">
      <span className="w-14 shrink-0 pt-0.5">
        <span className={`inline-flex rounded-md bg-selected px-1.5 py-0.5 text-[11px] font-medium ${KIND_COLOR[kind] ?? 'text-ghost'}`}>
          {t.journal.kind[kind as OutcomeKind] ?? fact.predicate}
        </span>
      </span>
      <div className="min-w-0 flex-1">
        {editing ? (
          <AutoTextarea
            originalValue={statement}
            value={draft}
            onChange={setDraft}
          />
        ) : (
          <div
            className={
              fact.status === 'live'
                ? `${OUTCOME_TEXT_CLASS} text-slate-100`
                : `${OUTCOME_TEXT_CLASS} text-ghost line-through`
            }
          >
            {statement}
          </div>
        )}
        {fact.why && (
          <div className="mt-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <button
                type="button"
                aria-expanded={evidenceOpen}
                aria-controls={evidenceId}
                onClick={() => setEvidenceOpen((open) => !open)}
                className="inline-flex items-center gap-1 bg-transparent p-0 text-xs leading-relaxed text-[rgb(var(--memory-edit-marker))] hover:text-foreground"
              >
                <ChevronRight className={`h-3 w-3 transition-transform motion-reduce:transition-none ${evidenceOpen ? 'rotate-90' : ''}`} aria-hidden="true" />
                {t.journal.viewEvidence}
              </button>
              {manualMarker}
            </div>
            <div id={evidenceId} hidden={!evidenceOpen} className="mt-2 border-l-2 border-line/70 py-1 pl-3 text-xs leading-relaxed text-ghost break-words">
              {fact.why}
            </div>
          </div>
        )}
        {!fact.why && manualMarker && <div className="mt-2">{manualMarker}</div>}
      </div>
      {editable && (
        <div
          className={`flex shrink-0 items-start gap-1.5 ${editing ? '' : 'opacity-0 transition group-hover:opacity-100 focus-within:opacity-100 [@media(pointer:coarse)]:opacity-100'}`}
        >
          {editing ? (
            <>
              <button
                type="button"
                onClick={save}
                disabled={!draft.trim() || editFact.isPending}
                className="rounded-lg border border-line p-1.5 text-brand-mint transition hover:border-brand-mint/50 disabled:opacity-50"
                aria-label={t.common.save}
                title={t.common.save}
                aria-busy={editFact.isPending}
              >
                {editFact.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                disabled={editFact.isPending}
                className="rounded-lg border border-line p-1.5 text-ghost transition hover:text-foreground disabled:opacity-50"
                aria-label={t.common.cancel}
                title={t.common.cancel}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={start}
                className="rounded-lg border border-line p-1.5 text-ghost transition hover:border-brand-cyan/50 hover:text-brand-cyan"
                aria-label={t.common.edit}
                title={t.common.edit}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setConfirming(true)}
                className="rounded-lg border border-line p-1.5 text-ghost transition hover:border-brand-rose/50 hover:text-brand-rose"
                aria-label={t.common.delete}
                title={t.common.delete}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirming}
        title={t.journal.deleteTitle}
        danger
        confirmLabel={t.common.delete}
        loading={deleteFact.isPending}
        body={
          <div className="space-y-3">
            <p>{t.journal.deleteBody}</p>
            <p className="rounded-lg border border-brand-rose/25 bg-brand-rose/10 px-3 py-2 text-brand-rose">
              {statement}
            </p>
          </div>
        }
        onClose={() => setConfirming(false)}
        onConfirm={() =>
          deleteFact.mutate(fact.id, {
            onSuccess: () => setConfirming(false),
            onError: (e) => toast.error(String((e as Error).message)),
          })
        }
      />
    </li>
  )
}

/** 原句作为淡底影保留；两层文字共同占位，避免删短草稿时裁掉原句。 */
function AutoTextarea({
  originalValue,
  value,
  onChange,
}: {
  originalValue: string
  value: string
  onChange: (v: string) => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const original = useRef(originalValue).current
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])

  return (
    <div className={`relative grid rounded-md bg-ink-700/70 ${OUTCOME_TEXT_CLASS}`}>
      <div aria-hidden="true" className="pointer-events-none min-w-0 select-none text-ghost opacity-50 [grid-area:1/1]">
        {original + '\u200b'}
      </div>
      <div aria-hidden="true" className="invisible min-w-0 [grid-area:1/1]">{value + '\u200b'}</div>
      <textarea
        ref={ref}
        rows={1}
        aria-label={t.journal.editConclusion}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="absolute inset-0 m-0 block h-full min-h-0 w-full resize-none overflow-hidden rounded-md border-0 bg-transparent p-0 text-slate-100 [font:inherit] whitespace-pre-wrap break-words outline-none ring-1 ring-brand-cyan/40 focus:ring-brand-cyan/60"
      />
    </div>
  )
}

/** 即使暂无结论，也保留从已有会话生成总结的入口。 */
function JournalEmpty() {
  return (
    <div>
      <PageEmptyState
        title={t.journal.emptyTitle}
        hint={t.journal.emptyHint}
        icon={<NotebookText className="h-6 w-6" />}
      />
      <BackfillSessions className="mt-7" />
    </div>
  )
}

/** 蒸馏一个已存在的会话。只有关闭会话才会写出结论，因此这是控制台触达那些从未通过
 *  agent 客户端关闭的历史记录的唯一入口。 */
function BackfillSessions({ className }: { className?: string }) {
  const sessions = useSessions({ limit: 8 })
  const closeSession = useCloseSession()
  const [pending, setPending] = useState<string | null>(null)

  const distil = (id: string) => {
    setPending(id)
    closeSession.mutate(id, {
      onSuccess: (r) => {
        setPending(null)
        if (r.outcomes > 0) {
          toast.success(t.journal.backfillDone(r.outcomes))
        } else {
          toast.info(t.journal.backfillNoNew)
        }
      },
      onError: (e) => {
        setPending(null)
        toast.error(String((e as Error).message))
      },
    })
  }

  if (!sessions.data?.sessions.length) return null

  return (
    <div className={className}>
      <ul className="space-y-4">
        {sessions.data.sessions.slice(0, 5).map((s) => (
          <li
            key={s.id}
            className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4 rounded-xl border border-line bg-ink-700/40 p-5 sm:px-6"
          >
            <div className="min-w-0">
              <div className="max-w-full truncate text-sm text-brand-cyan">
                {t.journal.sessionLabel} {s.id}
              </div>
              {s.last_event_at_h && <div className="mt-2 text-xs text-ghost">{s.last_event_at_h}</div>}
            </div>
            <Button
              variant="ghost"
              className="rounded-md px-4 py-2 text-sm"
              loading={pending === s.id}
              disabled={!!pending}
              onClick={() => distil(s.id)}
            >
              {t.journal.backfillButton}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  )
}
