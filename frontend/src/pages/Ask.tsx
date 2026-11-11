

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { CalendarDays, ChevronDown, ChevronUp, MessageSquare, Search, Sparkles, X, Zap } from 'lucide-react'

import { Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from '../components/ui'
import { useRecall } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import { copyText } from '../lib/clipboard'
import { toast } from '../components/Toast'
import { useSpacePreference } from '../store/preferences'

export default function Ask() {
  const recall = useRecall()
  const [query, setQuery] = useState('')
  const queryInput = useRef<HTMLInputElement>(null)
  const [chunks, setChunks] = useSpacePreference('ask', 'chunks')
  const chunksControl = useRef<HTMLDivElement>(null)
  const [asOf, setAsOf] = useState('')
  const [redactSensitive, setRedactSensitive] = useSpacePreference('ask', 'redactSensitive')
  const [showEvidence, setShowEvidence] = useState(false)

  useEffect(() => {
    const control = chunksControl.current
    if (!control) return
    const adjustChunks = (event: WheelEvent) => {
      if (event.ctrlKey || event.deltaY === 0) return
      event.preventDefault()
      setChunks((value) => Math.max(2, Math.min(12, value + (event.deltaY < 0 ? 1 : -1))))
    }
    // React 的 wheel 监听默认 passive；使用原生监听才能阻止页面跟着滚动。
    control.addEventListener('wheel', adjustChunks, { passive: false })
    return () => control.removeEventListener('wheel', adjustChunks)
  }, [setChunks])

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (q) {
      recall.mutate({
        query: q,
        answer: true,
        n_chunks: chunks,
        as_of: asOf ? new Date(`${asOf}T23:59:59`).getTime() / 1000 : undefined,
        redact_sensitive: redactSensitive,
      })
      setShowEvidence(false)
    }
  }

  const result = recall.data
  const blocks = result?.context_sections ?? []

  return (
    <div className="space-y-8">
      <PageHeader title={t.ask.title} />

      <Card>
        <form onSubmit={submit} className="space-y-6">
          <div className="flex flex-col gap-3 sm:flex-row">
          <div className="flex min-w-0 w-full flex-1 items-center gap-2 rounded-2xl border border-line bg-ink-700 px-3">
            <Search className="h-4 w-4 shrink-0 text-ghost" />
            <input
              ref={queryInput}
              aria-label={t.ask.placeholder}
              className="min-w-0 w-full bg-transparent py-3 text-sm outline-none placeholder:text-ghost/70"
              placeholder={t.ask.placeholder}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {query.length > 0 && (
              <button
                type="button"
                aria-label={t.ask.clearQuery}
                title={t.ask.clearQuery}
                onClick={() => {
                  setQuery('')
                  queryInput.current?.focus()
                }}
                className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-ghost transition hover:bg-selected hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <Button type="submit" loading={recall.isPending} disabled={!query.trim()} className="shrink-0 sm:self-stretch">
            {t.ask.searchBtn}
          </Button>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
            <div className="flex min-w-0 flex-wrap gap-3">
              {t.ask.examples.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  className="rounded-xl border border-line bg-ink-700/70 px-4 py-2 text-sm text-brand-cyan transition hover:border-brand-cyan/50"
                  onClick={() => setQuery(ex)}
                >
                  {ex}
                </button>
              ))}
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-x-8 gap-y-2 text-xs text-ghost sm:ml-auto">
            <div className="inline-flex h-9 min-w-0 items-center gap-2 text-sm">
              <span className="whitespace-nowrap">{t.ask.asOfControlLabel}</span>
              <div className="relative inline-flex h-9 items-center gap-2">
                <input
                  id="ask-as-of"
                  className="ask-date-input peer absolute inset-0 h-full w-full cursor-pointer opacity-0"
                  aria-label={t.ask.asOfLabel}
                  type="date"
                  value={asOf}
                  onChange={(e) => setAsOf(e.target.value)}
                  onClick={(e) => e.currentTarget.showPicker?.()}
                />
                <span aria-hidden="true" className="pointer-events-none whitespace-nowrap tabular-nums text-slate-100 peer-focus-visible:underline peer-focus-visible:underline-offset-4">{asOf || t.ask.asOfAny}</span>
                <CalendarDays aria-hidden="true" className="pointer-events-none h-4 w-4 shrink-0 text-ghost peer-focus-visible:text-brand-cyan" />
              </div>
            </div>
            <div className="inline-flex h-9 items-center gap-1 text-sm">
              <span className="whitespace-nowrap">{t.ask.chunksControlLabel}</span>
              <div ref={chunksControl} title={t.ask.chunksHint} className="inline-flex cursor-ns-resize items-center">
              <output
                id="ask-chunks"
                aria-label={t.ask.chunksLabel}
                className="inline-flex h-7 w-6 items-center justify-center text-sm tabular-nums text-slate-100"
              >
                {chunks}
              </output>
                <div className="flex flex-col">
                  <button
                    type="button"
                    aria-label={t.ask.increaseChunks}
                    onClick={() => setChunks((value) => Math.min(12, value + 1))}
                    className="grid h-3.5 w-4 cursor-pointer place-items-center text-ghost hover:text-brand-cyan focus-visible:!outline-none focus-visible:text-foreground"
                  >
                    <ChevronUp aria-hidden="true" className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    aria-label={t.ask.decreaseChunks}
                    onClick={() => setChunks((value) => Math.max(2, value - 1))}
                    className="grid h-3.5 w-4 cursor-pointer place-items-center text-ghost hover:text-brand-cyan focus-visible:!outline-none focus-visible:text-foreground"
                  >
                    <ChevronDown aria-hidden="true" className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={redactSensitive}
              aria-label={t.ask.redactSensitive}
              onClick={() => setRedactSensitive((value) => !value)}
              className="inline-flex min-h-[38px] shrink-0 select-none items-center gap-2.5 rounded-full text-sm text-ghost focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan"
            >
              <span aria-hidden="true" className="switch-track">
                <span className="switch-thumb" />
              </span>
              <span>{t.ask.redactSensitive}</span>
            </button>
          </div>
          </div>
        </form>
      </Card>

      {recall.isPending && <Spinner label={t.ask.searching} />}
      {recall.isError && <ErrorState message={(recall.error as Error).message} />}

      {result && (
        <>
          {result.answer_status === "model_not_configured" && <p role="status">{t.ask.answerUnconfigured}</p>}
          {result.answer_status === "model_failed" && <p role="status">{t.ask.answerFailed}</p>}
          {result.answer_status === "empty" && <p role="status">{t.ask.answerEmpty}</p>}
          {result.answer && (
            <Card className="!border-brand-mint/30 !bg-brand-mint/[0.06]">
              <div className="mb-1.5 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-brand-mint">
                <MessageSquare className="h-4 w-4" /> {t.ask.answer}
              </div>
              <p className="whitespace-pre-wrap text-[15px] leading-relaxed text-slate-100">{result.answer}</p>
              <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
                <Button
                  variant="ghost"
                  onClick={async () => {
                    try {
                      await copyText(result.answer || '')
                      toast.success(t.common.copied)
                    } catch {
                      toast.error(t.common.copyFailed)
                    }
                  }}
                >
                  {t.ask.copyAnswer}
                </Button>
              </div>
            </Card>
          )}

          <div className="rounded-xl border border-brand-cyan/20 bg-brand-cyan/5 p-4">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
              <span className="inline-flex items-center gap-2">
                <Zap className="h-5 w-5 text-brand-cyan" />
                <span className="font-semibold text-brand-cyan">{t.ask.lean(result.tokens_est)}</span>
              </span>
              {result.full_tokens != null && (
                <span className="text-ghost">
                  {t.ask.fullLabel} <span className="text-slate-200">≈{result.full_tokens.toLocaleString()}</span> tokens
                </span>
              )}
            </div>
            <p className="mt-1.5 text-xs text-ghost">{t.ask.leanNote}</p>
          </div>

          {blocks.length ? (
            <Card>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wider text-brand-cyan">{t.ask.evidenceTitle}</div>
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="ghost"
                    onClick={async () => {
                      try {
                        await copyText(result.context)
                        toast.success(t.common.copied)
                      } catch {
                        toast.error(t.common.copyFailed)
                      }
                    }}
                  >
                    {t.ask.copyContext}
                  </Button>
                  <Button variant="ghost" onClick={() => setShowEvidence((v) => !v)}>
                    {showEvidence ? t.ask.evidenceHide : t.ask.evidenceShow}
                  </Button>
                </div>
              </div>
              {showEvidence && (
              <div className="mt-4 gap-6 md:columns-2 xl:columns-3">
              {blocks.map((b, i) => (
                <section key={i} className="mb-6 break-inside-avoid rounded-xl border border-line bg-ink-700/50 p-4">
                  <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-brand-cyan">
                    {b.kind === 'facts' ? t.ask.sectionFacts : b.kind === 'profile' ? t.ask.sectionUserProfile : t.ask.sectionConversations}
                  </div>
                  <pre className="whitespace-pre-wrap break-words font-sans text-sm not-italic leading-relaxed text-slate-200">{b.text}</pre>
                </section>
              ))}
              </div>
              )}
            </Card>
          ) : (
            <EmptyState title={t.ask.emptyTitle} hint={t.ask.emptyHint} icon={<Sparkles className="h-6 w-6" />} />
          )}
        </>
      )}
    </div>
  )
}
