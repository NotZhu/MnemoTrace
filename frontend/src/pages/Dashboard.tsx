import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Database, ExternalLink, History, Lightbulb, MessagesSquare, Send, Sparkles } from 'lucide-react'

import { Button, Card, CardTitle, EmptyState, ErrorState, PageHeader, Spinner, StatCard } from '../components/ui'
import { toast } from '../components/Toast'
import { useMemories, useRemember, useProfile } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import { groupOutcomes, SessionOutcomes } from './Journal'
import { ProfileTag } from './Profile'
import type { ProfileResult } from '../types'

export default function Dashboard() {
  const memories = useMemories({ facts_limit: 6, episodes_limit: 0, status: 'live' })
  const { data, isLoading } = memories
  // 用户真正会读的"结论"。单独发一个查询，保证上面的统计不被过滤。
  // include_sensitive 与 counts.facts_outcomes（该统计忽略敏感标记）以及 Journal 页保持一致：
  // 否则把一条结论改写成会被 classify() 判为敏感的内容后，它会从这个列表消失但计数仍统计它，
  // 卡片就只剩一个标题、下面空空如也。
  const outcomes = useMemories({
    kind: 'outcomes',
    facts_limit: 12,
    episodes_limit: 0,
    status: 'live',
    include_sensitive: true,
  })
  const profile = useProfile()
  const remember = useRemember()
  const [text, setText] = useState('')
  const failed = [memories, outcomes, profile].filter(query => query.isError)
  const errorState = failed.length > 0 ? <ErrorState
    message={[...new Set(failed.map(query => (query.error as Error).message))].join('；')}
    onRetry={() => { failed.forEach(query => { void query.refetch() }) }}
  /> : null

  const submit = () => {
    const content = text.trim()
    if (!content) return
    remember.mutate(content, {
      onSuccess: (r) => {
        setText('')
        if (r.status === 'accepted' && r.operation_ids.length) toast.info('原文已保存，等待处理')
        else if (r.status === 'completed' && r.fact_ids?.length === 0) toast.info('原文已保存，事实未变更')
        else toast.success(r.status === 'completed' ? '核心处理完成' : '原文已保存')
      },
      onError: (e) => toast.error(String((e as Error).message)),
    })
  }

  if (isLoading) return <Spinner label={t.dashboard.loading} />
  if (!data) return errorState

  const c = data.counts
  const groups = groupOutcomes(outcomes.data?.facts ?? []).slice(0, 2)

  return (
    <div className="space-y-6">
      <PageHeader title={t.dashboard.title} />
      {errorState}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label={t.dashboard.statLive} value={c.facts_live} icon={<Database className="h-5 w-5" />} />
        <StatCard label={t.dashboard.statSuperseded} value={c.facts_superseded} icon={<History className="h-5 w-5" />} />
        <StatCard label={t.dashboard.statEpisodes} value={c.episodes} icon={<MessagesSquare className="h-5 w-5" />} />
        <StatCard label={t.dashboard.statOutcomes} value={c.facts_outcomes} icon={<Lightbulb className="h-5 w-5" />} />
      </div>

      <Card>
        <CardTitle>{t.dashboard.rememberTitle}</CardTitle>
        <textarea
          className="input min-h-[84px] resize-y"
          placeholder={t.dashboard.rememberPlaceholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="mt-3 flex items-center justify-end">
          <Button onClick={submit} loading={remember.isPending} disabled={!text.trim()}>
            <Send className="h-4 w-4" /> {t.dashboard.rememberButton}
          </Button>
        </div>
      </Card>

      {/* 展示的是 Journal（结论），不是碎片流：一个会话的结论才是人能读的东西。 */}
      <Card>
        <CardTitle hint={
          <Link to="/journal" className="inline-flex items-center gap-1.5 text-xs font-semibold text-brand-cyan hover:text-brand-mint">
            {t.dashboard.conclusionsAll} <ExternalLink className="h-3.5 w-3.5" />
          </Link>
        }>
          {t.dashboard.conclusionsTitle}
        </CardTitle>
        {/* 计数来自已加载的主查询，这样在结论本身还在请求中时，空状态不会闪烁。 */}
        {c.facts_outcomes > 0 ? (
          <div className="space-y-3">
            {groups.map((g, i) => (
              <SessionOutcomes key={`${g.session}-${i}`} group={g} className="!bg-ink-700/40 !p-4" />
            ))}
          </div>
        ) : !outcomes.isError ? (
          <EmptyState
            title={t.dashboard.conclusionsEmptyTitle}
            hint={t.dashboard.conclusionsEmptyHint}
            icon={<Lightbulb className="h-6 w-6" />}
          />
        ) : null}
      </Card>

      <Card>
        <CardTitle>{t.dashboard.personaTitle}</CardTitle>
        {profile.data ? (
          <StructuredProfileMini data={profile.data} />
        ) : !profile.isError ? (
          <EmptyState title={t.dashboard.personaEmptyTitle} hint={t.dashboard.personaEmptyHint} icon={<Sparkles className="h-6 w-6" />} />
        ) : null}
      </Card>
    </div>
  )
}

function StructuredProfileMini({ data }: { data: ProfileResult }) {
  const entries = Object.values(data.profile).flat()
  if (!entries.length) return <EmptyState title={t.dashboard.personaEmptyTitle} hint="到画像页面点击刷新画像。" />
  return <ul className="space-y-3">{entries.slice(0, 12).map((entry, index) => (
    <li key={index}><ProfileTag entry={entry} /></li>
  ))}</ul>
}
