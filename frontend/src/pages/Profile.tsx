import { RefreshCw } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { useProfile, useRefreshProfile, useRetryOperation } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import type { ProfileEntry, ProfileSection } from '../types'
import { PROFILE_SECTIONS } from '../generated/memory-contract'

const LABELS: Record<ProfileSection, string> = t.profile.sections

export function ProfileTag({ entry }: { entry: ProfileEntry }) {
  return (
    <span className="inline-flex items-baseline gap-2">
      <span className="text-sm leading-relaxed text-slate-100 whitespace-pre-wrap break-words">{entry.text}</span>
      {entry.fact_count >= 2 && (
        <span className="shrink-0 text-sm tabular-nums text-ghost"
          title={`关联 ${entry.fact_count} 条事实`} aria-label={`关联 ${entry.fact_count} 条事实`}>
          ×{entry.fact_count}
        </span>
      )}
    </span>
  )
}

export default function Profile() {
  const profile = useProfile()
  const refresh = useRefreshProfile()
  const retry = useRetryOperation()
  const data = profile.data
  const operationId = data?.operation_id
  const state = data?.refresh_state
  const running = ['pending', 'running', 'retry_wait'].includes(state ?? '') || refresh.isPending
  const failed = state === 'failed'
  const sections: [ProfileSection, ProfileEntry[]][] = data
    ? PROFILE_SECTIONS.map(section => [section, data.profile[section] ?? []]) : []
  const empty = sections.every(([, entries]) => entries.length === 0)
  if (profile.isLoading) return <Spinner label="正在读取画像…" />
  if (!data) return <ErrorState message={String(profile.error)} onRetry={() => { void profile.refetch() }} />

  return (
    <div className="space-y-6">
      <PageHeader title="用户画像" />
      {profile.isError && <ErrorState message={String(profile.error)} onRetry={() => { void profile.refetch() }} />}
      {refresh.isError && <ErrorState message={String(refresh.error)} onRetry={() => refresh.mutate()} />}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1 text-sm text-ghost">
            <p>{data.pending_fact_count} 条事实待更新</p>
            <p>{data.updated_at === null ? '尚未生成画像' : `上次生成于 ${new Date(data.updated_at * 1000).toLocaleString()}`}</p>
            {running && <p role="status">正在刷新画像…</p>}
            {data.needs_refresh && !running && <p>画像有待更新</p>}
          </div>
          <Button disabled={running} loading={refresh.isPending} onClick={() => refresh.mutate()}>
            <RefreshCw className="h-4 w-4" />刷新画像
          </Button>
        </div>
        {failed && <div className="mt-3 space-y-2">
          <p role="alert" className="text-sm text-brand-rose">画像刷新失败（{data.refresh_error}），现有可用内容继续显示。</p>
          <Button disabled={retry.isPending} onClick={() => operationId && retry.mutate(operationId)}>重试刷新</Button>
        </div>}
      </Card>
      {empty && <PageEmptyState title={data.updated_at === null ? '尚未生成画像' : '当前没有可展示的画像条目'}
        hint={data.updated_at === null ? '添加事实后，点击刷新画像生成。' : '可以补充事实后再次刷新画像。'} />}
      {sections.filter(([, entries]) => entries.length > 0).map(([section, entries]) => (
        <Card key={section}>
          <CardTitle>{LABELS[section]}</CardTitle>
          <ul className="space-y-3">
            {entries.map((entry, index) => <li key={index}><ProfileTag entry={entry} /></li>)}
          </ul>
        </Card>
      ))}
    </div>
  )
}
