import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  CheckCircle2,
  ChevronDown,
  Clipboard,
  ExternalLink,
  MessageSquare,
  PlugZap,
  RefreshCw,
  RotateCcw,
  Save,
  Sparkles,
  Terminal,
  Wand2,
} from 'lucide-react'

import { Badge, Button, Card, CardTitle, ErrorState, PageHeader, Spinner, cx } from '../components/ui'
import { toast } from '../components/Toast'
import { useAgentStatus, useHealth, usePolicy, useSessions, useSetPolicy } from '../hooks/queries'
import { copy as t } from '../lib/copy'

import { apiBaseUrl } from '../lib/api'
import { copyText } from '../lib/clipboard'
import { codexSetupCommand, defaultSetupShell, type SetupShell } from '../lib/codexSetupCommand'
import { useAuth } from '../store/auth'
import type { Policy } from '../types'

const CODEX_TEST_SESSION_KEY = 'mnemotrace:codex-test-session'

function initialCodexTestSession(): string {
  try {
    const saved = sessionStorage.getItem(CODEX_TEST_SESSION_KEY)
    if (saved?.trim()) return saved
  } catch {
    // 浏览器禁用存储时，仍允许在当前页面完成测试。
  }

  return generateCodexTestSession()
}

function generateCodexTestSession(): string {
  const suffix = typeof crypto !== 'undefined'
    ? Array.from(crypto.getRandomValues(new Uint8Array(4)), (byte) => byte.toString(16).padStart(2, '0')).join('')
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
  return `codex:onboarding:${suffix}`
}

export default function Settings() {
  const { data, isLoading, isError, error, refetch } = usePolicy()
  const session = useCodexSession()
  const status = useAgentStatus(session.sessionId)
  const save = useSetPolicy()

  const PROMPT_FIELDS: Array<{ key: keyof Policy; label: string; hint: string }> = [
    { key: 'extract_system', label: t.settings.promptExtractLabel, hint: t.settings.promptExtractHint },
    { key: 'digest_system', label: t.settings.promptDigestLabel, hint: t.settings.promptDigestHint },
    { key: 'persona_system', label: t.settings.promptPersonaLabel, hint: t.settings.promptPersonaHint },
  ]

  const [instruction, setInstruction] = useState('')
  const [prompts, setPrompts] = useState<Record<string, string>>({})
  const [dirty, setDirty] = useState(false)
  const [advanced, setAdvanced] = useState(false)

  useEffect(() => {
    if (data && !dirty) {
      setInstruction(data.policy.extract_instruction || '')
      setPrompts({
        extract_system: data.policy.extract_system || data.defaults.extract_system,
        digest_system: data.policy.digest_system || data.defaults.digest_system,
        persona_system: data.policy.persona_system || data.defaults.persona_system,
      })
    }
  }, [data, dirty])

  const isDefault = (key: keyof Policy) => prompts[key] === data?.defaults[key]

  const onSave = () => {
    if (!data) return
    // 仍处于默认值的 prompt 发送 ""，让它保持"使用默认"，而不是固化成一份覆盖副本。
    const patch: Partial<Policy> = {
      extract_instruction: instruction,
      extract_system: isDefault('extract_system') ? '' : prompts.extract_system,
      digest_system: isDefault('digest_system') ? '' : prompts.digest_system,
      persona_system: isDefault('persona_system') ? '' : prompts.persona_system,
    }
    save.mutate(patch, {
      onSuccess: () => {
        setDirty(false)
        toast.success(t.settings.saved)
      },
      onError: (e) => toast.error(String((e as Error).message)),
    })
  }

  const resetPrompt = (key: keyof Policy) => {
    if (!data) return
    setDirty(true)
    setPrompts((p) => ({ ...p, [key]: data.defaults[key] }))
  }

  return (
    <div className="space-y-6">
      <PageHeader title={t.settings.title} />
      {status.isError && <ErrorState message={`接入状态读取失败：${(status.error as Error).message}`} onRetry={() => { void status.refetch() }} />}

      <section aria-labelledby="codex-setup-heading" className="space-y-4">
        <h2 id="codex-setup-heading" className="text-base font-semibold text-slate-100">{t.settings.connectSectionTitle}</h2>
        <CodexSetupCard session={session} status={status} />
      </section>

      {!data ? <section aria-labelledby="memory-policy-heading" className="space-y-4">
        <h2 id="memory-policy-heading" className="text-base font-semibold text-slate-100">{t.settings.policySectionTitle}</h2>
        {isLoading ? <Spinner label={t.settings.loading} /> : isError ? (
          <ErrorState message={`策略读取失败：${(error as Error).message}`} onRetry={() => { void refetch() }} />
        ) : null}
      </section> : <section aria-labelledby="memory-policy-heading" className="space-y-4">
        {isError && <ErrorState message={`策略更新失败，保留当前草稿：${(error as Error).message}`} onRetry={() => { void refetch() }} />}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="memory-policy-heading" className="text-base font-semibold text-slate-100">{t.settings.policySectionTitle}</h2>
          <Button onClick={onSave} loading={save.isPending} disabled={!dirty}>
            <Save className="h-4 w-4" /> {t.settings.savePolicy}
          </Button>
        </div>
        {/* 主区块：记录什么 */}
        <Card>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h3 className="inline-flex items-center gap-2 text-sm font-medium tracking-wide text-foreground">
              <Wand2 className="h-4 w-4 text-brand-cyan" /> {t.settings.recordTitle}
            </h3>
          </div>
          <p className="mb-3 text-sm leading-relaxed text-ghost">{t.settings.recordDesc}</p>
          <textarea
            className="input settings-textarea min-h-[110px] resize-y leading-relaxed"
            placeholder={t.settings.recordPlaceholder}
            value={instruction}
            onChange={(e) => {
              setDirty(true)
              setInstruction(e.target.value)
            }}
          />
        </Card>

        {/* 高级：完整 prompt 编辑 */}
        <Card>
          <button
            type="button"
            className="flex w-full items-center justify-between"
            aria-expanded={advanced}
            aria-controls="advanced-memory-prompts"
            onClick={() => setAdvanced((v) => !v)}
          >
            <span className="inline-flex items-center gap-2 text-sm font-semibold text-brand-violet">
              <Sparkles className="h-4 w-4" /> {t.settings.advancedToggle}
            </span>
            <ChevronDown className={cx('h-4 w-4 text-ghost transition', advanced && 'rotate-180')} />
          </button>

          {advanced && (
            <div id="advanced-memory-prompts" className="mt-5 space-y-6">
              <p className="text-sm leading-relaxed text-ghost">{t.settings.advancedDesc}</p>
              {PROMPT_FIELDS.map((f) => (
                <div key={f.key}>
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <div>
                      <span className="text-sm font-medium text-slate-100">{f.label}</span>
                      <span className="ml-2">
                        {isDefault(f.key) ? <Badge>{t.settings.defaultBadge}</Badge> : <Badge tone="violet">{t.settings.customBadge}</Badge>}
                      </span>
                    </div>
                    <button
                      onClick={() => resetPrompt(f.key)}
                      disabled={isDefault(f.key)}
                      className="inline-flex items-center gap-1 text-xs text-ghost transition hover:text-brand-cyan disabled:opacity-40"
                    >
                      <RotateCcw className="h-3.5 w-3.5" /> {t.settings.resetDefault}
                    </button>
                  </div>
                  <p className="mb-2 text-xs text-ghost">{f.hint}</p>
                  <div className="group/copy relative">
                    <textarea
                      aria-label={f.label}
                      className="input settings-textarea block min-h-[140px] resize-y font-sans text-[12.5px] not-italic leading-relaxed"
                      value={prompts[f.key] ?? ''}
                      onChange={(e) => {
                        setDirty(true)
                        setPrompts((p) => ({ ...p, [f.key]: e.target.value }))
                      }}
                    />
                    <FloatingCopyButton
                      text={prompts[f.key] ?? ''}
                      label={`${t.settings.connectCopyPrompt}：${f.label}`}
                      disabled={!prompts[f.key]?.trim()}
                      bottomInset={16}
                      rightInset={16}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </section>}
    </div>
  )
}

function useCodexSession() {
  const [defaultSessionId, setDefaultSessionId] = useState(initialCodexTestSession)
  const [sessionId, setSessionId] = useState(defaultSessionId)
  useEffect(() => {
    try {
      sessionStorage.setItem(CODEX_TEST_SESSION_KEY, sessionId)
    } catch {
      // 存储不可用时保留内存中的 ID，不影响测试提示和结果查询。
    }
  }, [sessionId])
  const newSession = () => {
    const nextSessionId = generateCodexTestSession()
    setDefaultSessionId(nextSessionId)
    setSessionId(nextSessionId)
  }
  return { defaultSessionId, sessionId, setSessionId, newSession }
}

function CodexSetupCard({ session, status }: {
  session: ReturnType<typeof useCodexSession>
  status: ReturnType<typeof useAgentStatus>
}) {
  const [shell, setShell] = useState<SetupShell>(() => defaultSetupShell(
    typeof navigator === 'undefined' ? '' : navigator.platform || navigator.userAgent,
  ))
  const { defaultSessionId, sessionId, setSessionId, newSession } = session
  const sessionGhost = useRef<HTMLDivElement>(null)
  const apiUrl = apiBaseUrl()
  const health = useHealth()
  const sessions = useSessions({ q: 'codex', limit: 5 })
  const codexSessions = sessions.data?.sessions ?? []
  const spaceId = useAuth(state => state.spaceId)
  const feedCommand = `mnemotrace-watch --install --space-id '${(spaceId ?? '<SPACE_ID>').replace(/'/g, "''")}'`

  const command = codexSetupCommand(shell, apiUrl, spaceId ?? undefined)

  const testPrompt = t.settings.connectTestPrompt(sessionId)
  const serviceReady = !health.isError && !!health.data?.ready
  const serviceStatus = health.isError
    ? t.settings.connectServiceUnavailable
    : health.isLoading
      ? t.settings.connectServiceChecking
      : serviceReady
        ? t.settings.connectServiceReady
        : t.settings.connectServiceNotReady

  return (
    <Card>
      <CardTitle hint={
        <span role="status" className={cx('inline-flex items-center gap-1.5', serviceReady ? 'text-brand-mint' : 'text-ghost')}>
          <span aria-hidden="true">●</span> {serviceStatus}
        </span>
      }>
        <span className="inline-flex items-center gap-2">
          <PlugZap className="h-4 w-4 text-brand-mint" /> {t.settings.connectTitle}
        </span>
      </CardTitle>

      <div className="grid min-w-0 grid-cols-1 gap-4">
        <div className="min-w-0 space-y-4">
          <p className="text-sm leading-relaxed text-ghost">
            {t.settings.connectDescPre}
            <span className="text-slate-200">{t.settings.connectDescEmph}</span>
            {t.settings.connectDescPost}
          </p>

          <div className="rounded-xl border border-line bg-ink-700/60 p-4 pb-[13px]">
            <label htmlFor="codex-test-session" className="block text-sm font-semibold text-slate-100">{t.settings.connectSessionLabel}</label>
            <div className="settings-copy-surface group/copy relative mt-1.5 rounded-lg bg-ink-900/70">
              <div
                ref={sessionGhost}
                aria-hidden="true"
                className="pointer-events-none absolute inset-0 select-none overflow-hidden whitespace-pre rounded-lg p-3 text-xs leading-relaxed text-ghost opacity-50"
              >
                {defaultSessionId}
              </div>
              <input
                id="codex-test-session"
                className="relative w-full rounded-lg border-0 bg-transparent p-3 text-xs leading-relaxed text-slate-200 outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan/40"
                aria-describedby="codex-test-session-hint"
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
                onScroll={(event) => {
                  if (sessionGhost.current) sessionGhost.current.scrollLeft = event.currentTarget.scrollLeft
                }}
              />
              <FloatingCopyButton
                text={sessionId}
                label={t.settings.connectCopySession}
                disabled={!sessionId.trim()}
              />
            </div>
            <div className="mt-[13px] flex flex-wrap items-center justify-between gap-2">
              <span id="codex-test-session-hint" className="text-xs leading-relaxed text-ghost">
                {t.settings.connectSessionHint}
              </span>
              <button
                type="button"
                className="ml-auto inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold text-brand-cyan hover:text-brand-mint"
                onClick={() => {
                  newSession()
                  if (sessionGhost.current) sessionGhost.current.scrollLeft = 0
                }}
              >
                <RefreshCw className="h-3.5 w-3.5" /> {t.settings.connectNewSession}
              </button>
            </div>
          </div>

          <div className="rounded-xl border border-line bg-ink-700/60 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-100">
                <Terminal className="h-4 w-4 text-brand-cyan" /> {t.settings.connectStepInstall}
              </div>
              <div role="group" aria-label={t.settings.connectTerminal} className="relative top-1 ml-auto inline-grid shrink-0 grid-cols-2 gap-1 rounded-xl border border-control-line/60 bg-ink-700/70 p-1">
                {(['powershell', 'bash'] as const).map((option) => (
                  <button
                    key={option}
                    type="button"
                    aria-pressed={shell === option}
                    onClick={() => setShell(option)}
                    className={cx(
                      'whitespace-nowrap rounded-lg px-2.5 py-1 text-xs font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan',
                      shell === option ? 'bg-action text-foreground shadow-sm' : 'text-ghost hover:bg-ink-700 hover:text-foreground',
                    )}
                  >
                    {option === 'powershell' ? 'PowerShell' : 'Bash'}
                  </button>
                ))}
              </div>
            </div>
            <p className="mb-3 text-sm leading-relaxed text-ghost">{t.settings.connectInstallDesc}</p>
            <div className="settings-copy-surface group/copy relative rounded-lg bg-ink-900/70">
              <pre className="max-h-52 overflow-auto p-3 font-sans text-xs not-italic leading-relaxed text-slate-200">
                <code className="font-sans not-italic">{command}</code>
              </pre>
              <FloatingCopyButton text={command} label={t.settings.connectCopyCommand} bottomInset={12} />
            </div>
          </div>
        </div>

        <div className="min-w-0 space-y-4">
          <div className="rounded-xl border border-line bg-ink-700/60 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="inline-flex items-center gap-2 text-sm font-semibold text-slate-100">
                <MessageSquare className="h-4 w-4 text-brand-cyan" /> {t.settings.connectStepTest}
              </div>
            </div>
            <p className="mb-3 text-sm leading-relaxed text-ghost">{t.settings.connectTestDesc}</p>
            <div className="settings-copy-surface group/copy relative break-words rounded-lg bg-ink-900/70 p-3 text-xs leading-relaxed text-slate-200">
              <p>{testPrompt}</p>
              <FloatingCopyButton
                text={testPrompt}
                label={t.settings.connectCopyPrompt}
                disabled={!sessionId.trim()}
              />
            </div>
          </div>

          <div className="relative rounded-xl border border-line bg-ink-700/60 p-4">
            <details>
              <summary className="min-h-5 cursor-pointer pr-24 text-sm font-semibold text-slate-100">{t.settings.connectLiveTitle}</summary>
              <div className="mt-4 space-y-4 text-sm text-ghost">
                <div className="flex justify-between gap-3">
                  <span>{t.settings.connectNamespace}</span>
                  <span className="truncate text-right text-slate-200">{!status.isError ? status.data?.space_id ?? '—' : '—'}</span>
                </div>
                <div className="flex justify-between gap-3">
                  <span>{t.settings.connectSessionEpisodes}</span>
                  <span className="text-slate-200">{!status.isError ? status.data?.session?.episodes ?? '—' : '—'}</span>
                </div>
              </div>
            </details>
            <button
              type="button"
              className="absolute right-4 top-4 inline-flex min-h-5 shrink-0 items-center gap-1.5 text-xs font-semibold text-brand-cyan hover:text-brand-mint"
              onClick={() => {
                status.refetch()
                sessions.refetch()
              }}
            >
              <RefreshCw className="h-3.5 w-3.5" /> {t.settings.connectRefresh}
            </button>
          </div>

          <details className="rounded-xl border border-line bg-ink-700/60 p-4">
            <summary className="cursor-pointer text-sm font-semibold text-slate-100">
              {t.settings.connectRecentSessions}
            </summary>
            <div className="mt-3 space-y-2">
              {sessions.isLoading ? (
                <p className="text-xs leading-relaxed text-ghost">{t.settings.connectSessionsLoading}</p>
              ) : sessions.isError ? (
                <ErrorState message={t.settings.connectSessionsError} onRetry={() => { void sessions.refetch() }} />
              ) : codexSessions.length === 0 ? (
                <p className="text-xs leading-relaxed text-ghost">{t.settings.connectSessionsEmpty}</p>
              ) : (
                codexSessions.map((row) => (
                  <div key={row.id} className="rounded-lg bg-ink-700/70 px-3 py-2 text-xs text-ghost">
                    <div className="truncate font-medium text-slate-200">{row.id}</div>
                    <div className="mt-1 flex flex-wrap gap-2">
                      <span>{t.conversations.sessionCardEpisodes(row.episodes)}</span>
                      <span>{t.conversations.sessionCardFacts(row.facts_added)}</span>
                      {row.last_event_at_h && <span>{t.conversations.sessionCardLast(row.last_event_at_h)}</span>}
                    </div>
                  </div>
                ))
              )}
            </div>
            <Link
              to="/conversations"
              className="mt-4 inline-flex items-center gap-1.5 text-xs font-semibold text-brand-cyan hover:text-brand-mint"
            >
              {t.settings.connectOpenSessions} <ExternalLink className="h-3.5 w-3.5" />
            </Link>
          </details>
        </div>
      </div>
      <details className="mt-4 rounded-xl border border-line bg-ink-700/60 p-4">
        <summary className="cursor-pointer text-sm font-semibold text-slate-100">
          {t.settings.connectFeedTitle}
        </summary>
        {/* 只有 24 小时内导入过才显示绿色，避免把陈旧记录显示为健康状态。 */}
        <div className="mt-3">
          <StatusTile
            label={t.settings.connectFeedLabel}
            value={
              status.isError || !status.data ? '—' : status.data?.feed?.last_fed_at_h
                ? t.settings.connectFedAt(status.data.feed.last_fed_at_h, status.data.feed.sessions)
                : t.settings.connectNeverFed
            }
            ok={!status.isError && !!status.data?.feed?.last_fed_at && Date.now() / 1000 - status.data.feed.last_fed_at < 86400}
          />
        </div>
        <div className="mt-3">
          <p className="mb-1.5 text-sm leading-relaxed text-ghost">{t.settings.connectFeedCommandLabel}</p>
          <div className="settings-copy-surface group/copy relative rounded-lg bg-ink-900/70">
            <pre className="overflow-auto p-3 font-sans text-xs not-italic leading-relaxed text-slate-200">
              <code className="font-sans not-italic">{feedCommand}</code>
            </pre>
            <FloatingCopyButton text={feedCommand} label={t.settings.connectCopyCommand} />
          </div>
        </div>
      </details>
    </Card>
  )
}

function FloatingCopyButton({ text, label, disabled = false, bottomInset = 8, rightInset = 8 }: {
  text: string
  label: string
  disabled?: boolean
  bottomInset?: number
  rightInset?: number
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      style={{ bottom: bottomInset, right: rightInset }}
      className="pointer-events-none absolute inline-flex items-center gap-1 whitespace-nowrap rounded-md border border-line bg-ink-700 px-2 py-1 text-xs text-brand-cyan opacity-0 shadow-sm transition group-hover/copy:pointer-events-auto group-hover/copy:opacity-100 group-focus-within/copy:pointer-events-auto group-focus-within/copy:opacity-100 hover:text-brand-mint focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan disabled:cursor-not-allowed [@media(hover:none)]:pointer-events-auto [@media(hover:none)]:opacity-100"
      onClick={() => copy(text, t.common.copied, t.common.copyFailed)}
    >
      <Clipboard className="h-3.5 w-3.5" /> {t.common.copy}
    </button>
  )
}

function StatusTile({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="rounded-xl border border-line bg-ink-700/60 p-3">
      <div className="text-xs text-ghost">{label}</div>
      <div className={cx('mt-1 flex items-center gap-2 text-sm font-semibold', ok ? 'text-brand-mint' : 'text-slate-200')}>
        {ok && <CheckCircle2 className="h-4 w-4" />}
        {value}
      </div>
    </div>
  )
}

async function copy(text: string, successMessage: string, errorMessage: string) {
  try {
    await copyText(text)
    toast.success(successMessage)
  } catch {
    toast.error(errorMessage)
  }
}
