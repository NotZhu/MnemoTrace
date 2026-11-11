

import { CategoryBadges } from '../components/CategoryEditor'
import { useRef, useState } from 'react'
import { CircleAlert, Send, ShieldAlert } from 'lucide-react'

import { Badge, Button, Card, CardTitle, EmptyState, ErrorState, PageHeader, Spinner } from '../components/ui'
import { Modal } from '../components/Modal'
import { SessionPicker } from '../components/SessionPicker'
import { OperationProgress } from '../components/OperationProgress'
import { toast } from '../components/Toast'
import {
  useAgentStatus,
  useOrganizeSession,
  useMemoryPages,
  useRememberMessages,
  useSessionReport,
  useSessions,
} from '../hooks/queries'
import { useDebouncedSearch } from '../hooks/useDebouncedSearch'
import { copy as t } from '../lib/copy'
import { truncate } from '../lib/format'

const SESSION_ACTION_CLASS = 'whitespace-nowrap border-0 bg-transparent p-0 text-[11px] text-ghost transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand-cyan disabled:cursor-wait disabled:opacity-50'

export default function Conversations() {
  const { data, isLoading, isError, error, refetch, loadMore, isFetching } = useMemoryPages({
    facts_limit: 0,
    episodes_limit: 20,
    include_sensitive: true,
  })
  const [sessionId, setSessionId] = useState('default')
  const currentSession = sessionId.trim() || 'default'
  const sessionEpoch = useRef(0)
  const [sessionRequest, setSessionRequest] = useState(() => new AbortController())
  const remember = useRememberMessages(sessionRequest.signal)
  const [role, setRole] = useState<'user' | 'assistant'>('user')
  const [mode, setMode] = useState<'live' | 'historical'>('live')
  const [pendingInput, setPendingInput] = useState<{identity: string; id: string} | null>(null)
  const status = useAgentStatus(currentSession)
  const organizeSession = useOrganizeSession(sessionRequest.signal)
  const [text, setText] = useState('')
  const [sessionSearch, setSessionSearch] = useState('')
  const [searchComposing, setSearchComposing] = useState(false)
  const sessionQuery = useDebouncedSearch(sessionSearch, searchComposing, 500)
  const sessions = useSessions({ limit: 12, q: sessionQuery }, { keepPreviousData: true })
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [auditSession, setAuditSession] = useState<string | null>(null)
  const [reportOffset, setReportOffset] = useState(0)
  const report = useSessionReport(auditSession, false, reportOffset)

  const showReport = (id: string | null) => {
    setAuditSession(id)
    setReportOffset(0)
  }

  const chooseSession = (value: string) => {
    if ((value.trim() || 'default') !== currentSession) {
      sessionEpoch.current += 1
      sessionRequest.abort()
      setSessionRequest(new AbortController())
      if (pendingInput) setText('')
      setPendingInput(null)
      showReport(null)
    }
    setSessionId(value)
  }

  const submit = () => {
    const content = text.trim()
    if (!content) return
    const identity = JSON.stringify([currentSession, content, role, mode])
    const messageId = pendingInput?.identity === identity ? pendingInput.id : crypto.randomUUID()
    setPendingInput({identity, id: messageId})
    const epoch = sessionEpoch.current
    remember.mutate({messages: [{message_id: messageId, role, content}], sessionId: currentSession, mode}, {
      onSuccess: (r) => {
        if (epoch !== sessionEpoch.current) return
        setText('')
        setPendingInput(null)
        if (r.status === 'accepted' && r.operation_ids.length) toast.info('原文已保存，正在等待模型抽取')
        else toast.success('消息已保存')
      },
      onError: (e) => {
        if (epoch === sessionEpoch.current) toast.error(String((e as Error).message))
      },
    })
  }

  const finishSession = (id: string) => {
    const epoch = sessionEpoch.current
    organizeSession.mutate(id, {
      onSuccess: (r) => {
        if (epoch !== sessionEpoch.current) return
        if (r.ok) toast.success('会话核心处理完成')
        else toast.info(r.status === 'timed_out' ? '等待超时，任务继续处理' : '处理未完成，请查看操作进度')
        showReport(id)
      },
      onError: (e) => {
        if (epoch === sessionEpoch.current) toast.error(String((e as Error).message))
      },
    })
  }


  const episodes = data?.episodes ?? []
  const reportNextOffset = report.data?.facts_page?.next_offset

  return (
    <div className="space-y-6">
      <PageHeader title={t.conversations.title} />
      {status.isError && <ErrorState message={`会话状态读取失败：${(status.error as Error).message}`} onRetry={() => { void status.refetch() }} />}

      <Card className="!pb-3.5">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <h3 className="text-sm font-medium tracking-wide text-foreground">{t.conversations.rememberTitle}</h3>
          {status.data && !status.isError && (
            <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-x-2 gap-y-1 text-xs text-ghost">
              <span className="min-w-0 break-all">{t.conversations.statusNamespace(status.data.space_id)}</span>
              <span aria-hidden="true">·</span>
              <span className="whitespace-nowrap">{t.conversations.statusFacts(status.data.counts.facts_live)}</span>
            </div>
          )}
        </div>
        <textarea
          rows={3}
          className="input min-h-[82px] resize-y"
          placeholder={t.conversations.rememberPlaceholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit()
          }}
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 pl-2">
          <div className="flex min-w-0 max-w-full items-center gap-5">
            <div className="min-w-0 max-w-full sm:max-w-[420px]">
              <SessionPicker value={sessionId} onChange={chooseSession} />
            </div>
            <div className="shrink-0">
              <select aria-label="消息角色" className="input" value={role}
                onChange={(e) => setRole(e.target.value as 'user' | 'assistant')}>
                <option value="user">用户消息</option><option value="assistant">AI 完整回复</option>
              </select>
              <select aria-label="消息模式" className="input" value={mode}
                onChange={(e) => setMode(e.target.value as 'live' | 'historical')}>
                <option value="live">实时</option><option value="historical">历史</option>
              </select>
            </div>
          </div>
          <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-3">
            {status.data && !status.isError && (status.data.session.episodes_pending > 0 || status.data.consolidation_backlog) && (
              <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1 text-xs text-ghost">
                {status.data.session.episodes_pending > 0 && (
                  <span className="text-brand-amber">{t.conversations.statusPending(status.data.session.episodes_pending)}</span>
                )}
                {status.data.consolidation_backlog && (
                  <span className="inline-flex items-center gap-1 text-brand-amber">
                    <CircleAlert className="h-3.5 w-3.5" /> {t.conversations.statusBacklog}
                  </span>
                )}
              </div>
            )}
            <Button onClick={submit} loading={remember.isPending} disabled={!text.trim()}>
              <Send className="h-4 w-4" /> {t.conversations.rememberButton}
            </Button>
          </div>
        </div>
      </Card>

      <OperationProgress key={currentSession} sessionId={currentSession} sessionSignal={sessionRequest.signal} />

      <Card>
        <CardTitle>{t.conversations.sessionsTitle}</CardTitle>
        <input
          className="input mb-3"
          value={sessionSearch}
          onChange={(e) => setSessionSearch(e.target.value)}
          onCompositionStart={() => setSearchComposing(true)}
          onCompositionEnd={() => setSearchComposing(false)}
          placeholder={t.conversations.sessionsSearch}
        />
        {sessions.isLoading && <Spinner label={t.conversations.sessionsLoading} />}
        {sessions.isError && <ErrorState message={`会话索引读取失败${sessions.data ? '，显示上次结果' : ''}：${(sessions.error as Error).message}`} onRetry={() => { void sessions.refetch() }} />}
        {sessions.data && (
          sessions.data.sessions.length ? (
            <ul className="grid gap-2 md:grid-cols-2">
              {sessions.data.sessions.map((s) => (
                <li key={s.id} className="rounded-xl border border-line bg-ink-700/40 p-3">
                  <div className="max-w-full truncate text-xs text-slate-200" title={s.id}>
                    {t.conversations.sessionCardTitle(s.id)}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-ghost">
                    <span>{t.conversations.sessionCardEpisodes(s.episodes)} · {t.conversations.sessionCardFacts(s.facts_added)}</span>
                    {s.episodes_pending > 0 && <span className="text-brand-amber">{t.conversations.sessionCardPending(s.episodes_pending)}</span>}
                    {s.facts_sensitive > 0 && <span className="text-brand-rose">{t.conversations.sessionCardSensitive(s.facts_sensitive)}</span>}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-[11px] text-ghost">
                    {s.last_event_at_h && <span className="whitespace-nowrap">{t.conversations.sessionCardLast(s.last_event_at_h)}</span>}
                    <div className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
                      <button type="button" className={SESSION_ACTION_CLASS} title={t.conversations.auditSessionHint} onClick={() => showReport(s.id)}>
                        {t.conversations.auditSession}
                      </button>
                      <button type="button" className={SESSION_ACTION_CLASS} title={t.conversations.useSessionHint} onClick={() => chooseSession(s.id)}>
                        {t.conversations.useSession}
                      </button>
                      <button type="button" className={SESSION_ACTION_CLASS}
                        title={t.conversations.organizeSessionHint}
                        disabled={organizeSession.isPending}
                        aria-busy={organizeSession.isPending && organizeSession.variables === s.id}
                        onClick={() => finishSession(s.id)}>
                        {organizeSession.isPending && organizeSession.variables === s.id ? t.conversations.closingSession : t.conversations.organizeSession}
                      </button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title={t.conversations.sessionsEmptyTitle} hint={t.conversations.sessionsEmptyHint} />
          )
        )}
      </Card>

      <Card>
        <CardTitle hint={data ? t.conversations.episodesHint(data.episodes_page?.total ?? episodes.length) : undefined}>{t.conversations.episodesTitle}</CardTitle>
        {isError && <ErrorState message={`${data ? '对话记录更新失败，显示上次结果：' : ''}${(error as Error).message}`} onRetry={() => { void refetch() }} />}
        {!data ? (isLoading ? <Spinner label={t.conversations.loading} /> : null) : episodes.length ? (
          <>
          <ul className="gap-3 md:columns-2">
            {episodes.map((e) => {
              const key = e.id
              const isOpen = expanded[key]
              const long = e.content.length > 320
              return (
                <li key={key} className="mb-3 break-inside-avoid rounded-xl border border-line bg-ink-700/40 p-4">
                  <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[11px] text-ghost">
                    <span className="tabular-nums text-brand-cyan">{e.date}</span>
                    <span>{e.role === 'assistant' ? 'AI 回复' : e.role === 'user' ? '用户消息' : e.role}</span>
                    {e.mode === 'historical' && <span>历史</span>}
                    <span className="inline-flex min-w-0 max-w-full whitespace-normal break-all rounded bg-ink-700/60 px-1.5 py-px">{e.session}</span>
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
                    {isOpen || !long ? e.content : truncate(e.content, 320)}
                  </p>
                  {long && (
                    <button
                      className="mt-2 text-xs font-medium text-brand-cyan hover:underline"
                      onClick={() => setExpanded((v) => ({ ...v, [key]: !isOpen }))}
                    >
                      {isOpen ? t.conversations.collapseEntry : t.conversations.expandEntry}
                    </button>
                  )}
                  {e.digest && (
                    <p className="mt-2 border-l-2 border-brand-violet/40 pl-3 text-xs leading-relaxed text-ghost">
                      {t.conversations.digestPrefix}
                      {e.coverage?.partial && <span>（仅覆盖可见首尾）</span>}
                      {e.digest}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
          {data.episodes_page?.has_more && (
            <div className="mt-4 flex justify-center">
              <Button variant="ghost" disabled={isFetching || isError} onClick={() => { void loadMore() }}>
                {t.conversations.loadMore}
              </Button>
            </div>
          )}
          </>
        ) : (
          <EmptyState title={t.conversations.emptyTitle} hint={t.conversations.emptyHint} />
        )}
      </Card>

      <Modal
        open={!!auditSession}
        onClose={() => showReport(null)}
        title={auditSession ? t.conversations.auditTitle(auditSession) : t.conversations.auditTitle('')}
      >
        {report.isLoading && <Spinner label={t.conversations.auditLoading} />}
        {report.isError && <ErrorState message={`会话报告读取失败${report.data ? '，显示上次结果' : ''}：${(report.error as Error).message}`} onRetry={() => { void report.refetch() }} />}
        {report.data && (
          <div className="space-y-4 text-sm">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <AuditMetric label={t.conversations.auditEpisodes} value={report.data.episodes} />
              <AuditMetric label={t.conversations.auditFacts} value={report.data.facts_added} />
              <AuditMetric label={t.conversations.auditPending} value={report.data.episodes_pending} />
            </div>
            {report.data.facts_redacted > 0 && (
              <p className="inline-flex items-center gap-2 rounded-lg border border-brand-amber/30 bg-brand-amber/10 px-3 py-2 text-xs text-brand-amber">
                <ShieldAlert className="h-3.5 w-3.5" /> {t.conversations.auditRedacted(report.data.facts_redacted)}
              </p>
            )}
            {report.data.facts.length ? (
              <ul className="max-h-[360px] space-y-2 overflow-auto pr-1">
                {report.data.facts.map((f) => (
                  <li key={f.id} className="rounded-lg border border-line bg-ink-700/50 p-3">
                    <div className="break-words text-slate-100">{f.text}</div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-ghost">
                      <Badge tone={f.status === 'live' ? 'live' : 'old'}>{f.status}</Badge>
                      {f.categories && f.categories_source && <CategoryBadges categories={f.categories} categories_source={f.categories_source} />}
                      {f.sensitive && (
                        <span className="inline-flex items-center gap-1 rounded bg-brand-rose/15 px-1.5 py-px text-brand-rose">
                          <ShieldAlert className="h-3 w-3" /> {t.conversations.auditSensitive}
                        </span>
                      )}
                      <span>{f.valid_at}</span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : reportOffset === 0 ? (
              <EmptyState title={t.conversations.auditEmptyTitle} hint={t.conversations.auditEmptyHint} />
            ) : null}
            {(reportOffset > 0 || report.data.facts_page?.has_more) && (
              <div className="flex justify-end gap-2">
                {reportOffset > 0 && <Button variant="ghost" onClick={() => setReportOffset(0)}>回到首页</Button>}
                {reportNextOffset != null && (
                  <Button variant="ghost" onClick={() => setReportOffset(reportNextOffset)}>下一页</Button>
                )}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  )
}

function AuditMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-line bg-ink-700/50 px-3 py-2">
      <div className="text-lg font-semibold leading-none text-slate-100">{value}</div>
      <div className="mt-1 text-[11px] text-ghost">{label}</div>
    </div>
  )
}
