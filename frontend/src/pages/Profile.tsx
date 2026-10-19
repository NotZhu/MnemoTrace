

import { CircleCheck, HelpCircle, Trash2, UserRound } from 'lucide-react'

import { Card, CardTitle, ErrorState, PageHeader, Spinner, cx } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { toast } from '../components/Toast'
import { useAddFact, useDeleteFact, useMemories, useStructuredProfile } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import type { Evidence, ProfileItem } from '../types'

const ICON_COLOR = 'text-brand-cyan'

export function EvidenceChip({ e }: { e: Evidence }) {
  if (e.source_count === 0 && e.kind !== 'reinforced') return null

  return (
    <span className="inline-flex shrink-0 items-center gap-1.5">
      {e.source_count > 0 && (
        <span
          className="text-sm font-normal tabular-nums text-ghost"
          title={t.profile.evidenceTitle}
          aria-label={`${t.profile.evidenceTitle}：${e.source_count}`}
        >
          x{e.source_count}
        </span>
      )}

      {e.kind === 'reinforced' && (
        <span className="text-[10px] text-ghost" title={t.profile.evidenceReinforcedHint}>
          {t.profile.evidenceReinforced}
        </span>
      )}
    </span>
  )
}

function PrefChip({ it }: { it: ProfileItem }) {
  const dislike = it.polarity === 'dislike'
  return (
    <span className="inline-flex min-w-0 max-w-full items-center gap-1.5">
      <span
        title={dislike ? t.profile.iconHints.dislike : t.profile.iconHints.like}
        className={cx(
          'min-w-0 max-w-full break-all rounded-lg px-2.5 py-1 text-sm',
          dislike ? 'bg-brand-rose/10 text-brand-rose' : 'bg-brand-mint/10 text-brand-mint',
        )}
      >
        {it.item}
      </span>
      <EvidenceChip e={it.evidence} />
    </span>
  )
}

export default function Profile() {
  const { data, isLoading, isError, error } = useStructuredProfile()
  const memories = useMemories({ facts_limit: 240, episodes_limit: 0 })
  const addFact = useAddFact()
  const deleteFact = useDeleteFact()

  if (isLoading) return <Spinner label={t.profile.loading} />
  if (isError) return <ErrorState message={(error as Error).message} />
  if (!data) return null

  const confirm = (it: ProfileItem) =>
    addFact.mutate(
      { subject: it.subject, predicate: it.predicate, object: it.object },
      {
        onSuccess: () => toast.success(t.profile.confirmed(it.item)),
        onError: (e) => toast.error(String((e as Error).message)),
      },
    )

  const dismiss = (it: ProfileItem) =>
    deleteFact.mutate(it.fact_id, {
      onSuccess: () => toast.success(t.profile.dismissed),
      onError: (e) => toast.error(String((e as Error).message)),
    })

  const prefCats = Object.entries(data.preferences)
  const empty = data.counts.basic + data.counts.preferences + data.counts.tentative + data.counts.habits === 0

  return (
    <div className={empty ? 'flex min-h-0 flex-1 flex-col' : 'space-y-6'}>
      <PageHeader title={t.profile.title} />

      {empty && (
        <PageEmptyState title={t.profile.emptyTitle} hint={t.profile.emptyHint} icon={<UserRound className="h-6 w-6" />} />
      )}

      {/* 基本信息 */}
      {data.basic.length > 0 && (
        <Card>
          <CardTitle hint={t.profile.basicHint}>{t.profile.basicTitle}</CardTitle>
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
            {data.basic.map((b) => (
              <div key={b.field}>
                <div className="text-xs text-ghost">{b.label}</div>
                <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 break-words text-sm text-slate-100">{b.value}</span>
                  <EvidenceChip e={b.evidence} />
                </div>
                <HistoricalSlot factId={b.fact_id} facts={memories.data?.facts ?? []} />
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 按类别分组的偏好 */}
      {prefCats.length > 0 && (
        <Card>
          <CardTitle>{t.profile.prefsTitle}</CardTitle>
          <div className="space-y-4">
            {prefCats.map(([cat, items]) => (
              <div key={cat} className="min-w-0 rounded-2xl border border-line bg-ink-700/40 p-4">
                <h3 className="mb-2 text-xs font-medium tracking-wider text-brand-cyan">{cat}</h3>
                <div className="flex flex-wrap gap-2">
                  {items.map((it) => (
                    <PrefChip key={it.fact_id} it={it} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 习惯 */}
      {data.habits.length > 0 && (
        <Card>
          <CardTitle>{t.profile.habitsTitle}</CardTitle>
          <ul className="flex flex-wrap gap-2">
            {data.habits.map((h) => (
              <li key={h.fact_id} className="inline-flex min-w-0 max-w-full items-center gap-1.5">
                <span title={t.profile.iconHints.habit} className="min-w-0 max-w-full break-all rounded-lg bg-ghost/10 px-2.5 py-1 text-sm text-ghost">
                  {h.text}
                </span>
                <EvidenceChip e={h.evidence} />
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* 待定项 —— L1→L2 的晋升门槛，仅展示 */}
      {data.tentative.length > 0 && (
        <Card>
          <CardTitle hint={t.profile.tentativeHint}>
            <span className="inline-flex items-center gap-2">
              <span role="img" aria-label={t.profile.iconPending} title={t.profile.iconHints.pending} className="inline-flex shrink-0">
                <HelpCircle aria-hidden="true" className={`h-[18px] w-[18px] ${ICON_COLOR}`} />
              </span>
              {t.profile.tentativeTitle(data.tentative.length)}
            </span>
          </CardTitle>
          <p className="mb-3 text-sm text-ghost">
            {t.profile.tentativeDescPre}
            <span className="text-slate-200">{t.profile.tentativeDescEmph}</span>
            {t.profile.tentativeDescPost}
          </p>
          <ul className="divide-y divide-line">
            {data.tentative.map((it) => (
              <li key={it.fact_id} className="flex flex-col gap-2 py-2.5 sm:flex-row sm:items-center sm:gap-3">
                <span className={cx('inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium', it.polarity === 'like' ? 'text-brand-mint' : 'text-ghost line-through')}>
                  {it.polarity === 'like' ? t.profile.like : t.profile.dislike}
                </span>
                <span className="min-w-0 flex-1 break-all text-sm text-slate-100">
                  {it.item} <span className="text-xs text-ghost">· {it.category}</span>
                </span>
                <EvidenceChip e={it.evidence} />
                <button
                  onClick={() => confirm(it)}
                  title={t.profile.iconHints.confirm}
                  className={`inline-flex items-center gap-1 rounded-lg border border-brand-cyan/30 bg-transparent px-2.5 py-1 text-xs ${ICON_COLOR} transition hover:border-brand-cyan/50`}
                >
                  <CircleCheck aria-hidden="true" className="h-[18px] w-[18px]" /> {t.common.confirm}
                </button>
                <button
                  onClick={() => dismiss(it)}
                  className={`rounded-lg border border-line bg-transparent p-1.5 ${ICON_COLOR} transition hover:border-brand-cyan/50`}
                  title={t.profile.iconHints.delete}
                  aria-label={t.profile.deleteCandidate}
                >
                  <Trash2 className="h-[18px] w-[18px]" />
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

function HistoricalSlot({ factId, facts }: { factId: string; facts: Array<{ id: string; subject: string; predicate: string; object: string; status: string; invalid_at: string | null }> }) {
  const current = facts.find((f) => f.id === factId)
  if (!current) return null
  const old = facts.filter(
    (f) =>
      f.id !== current.id &&
      f.status !== 'live' &&
      f.subject === current.subject &&
      f.predicate === current.predicate,
  )
  if (!old.length) return null
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-[11px] text-brand-cyan">{t.profile.historicalValues}</summary>
      <ul className="mt-1 space-y-1">
        {old.slice(0, 4).map((f) => (
          <li key={f.id} className="break-words text-[11px] text-ghost">
            {f.object}
            {f.invalid_at ? ` · ${f.invalid_at}` : ''}
          </li>
        ))}
      </ul>
    </details>
  )
}
