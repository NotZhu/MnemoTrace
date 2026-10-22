import { useEffect, useLayoutEffect, useRef, useState, type FocusEvent } from 'react'
import { AlertTriangle, Check, Eraser, Loader2, Pencil, RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { ConfirmDialog } from '../components/Modal'
import { toast } from '../components/Toast'
import { useAudit, useClearSlot, useDeleteFact, useEditFact } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import type { AuditFinding } from '../types'

const FINDING_TEXT_CLASS = 'text-sm leading-relaxed whitespace-pre-wrap break-words'
const RECHECK_MIN_FEEDBACK_MS = 600
const OUTCOME_PREDICATES = new Set(['decision', 'finding', 'lesson', 'open_question'])
type AttributeDraft = { subject: string; predicate: string; object: string }
// 默认处理优先级；具体内容仍需人工核对，颜色不代表已证实的错误。
const KIND_PRESENTATION: Record<AuditFinding['kind'], { priority: number; className: string }> = {
  machine_token: { priority: 1, className: 'bg-brand-amber/15 text-brand-amber' },
  empty_value: { priority: 0, className: 'bg-brand-rose/15 text-brand-rose' },
  unreduced_claim: { priority: 2, className: 'fact-blue-tag' },
  slot_overflow: { priority: 1, className: 'bg-brand-amber/15 text-brand-amber' },
  fragment: { priority: 1, className: 'bg-brand-amber/15 text-brand-amber' },
  dangling_subject: { priority: 1, className: 'bg-brand-amber/15 text-brand-amber' },
  code_artifact: { priority: 2, className: 'fact-blue-tag' },
  embedder_blind: { priority: 0, className: 'bg-brand-rose/15 text-brand-rose' },
  orphan_entity: { priority: 3, className: 'bg-ghost/10 text-ghost' },
}

const findingPriority = (kind: string) => KIND_PRESENTATION[kind as AuditFinding['kind']]?.priority ?? 3

function FindingLabel({ kind }: { kind: AuditFinding['kind'] }) {
  return (
    <span className={`inline-flex rounded-md px-2 py-0.5 text-xs font-medium ${KIND_PRESENTATION[kind]?.className ?? 'bg-ghost/10 text-ghost'}`}>
      {t.health.kind[kind] ?? kind}
    </span>
  )
}

/** 审计是待确认的线索；具体内容和修复操作一起呈现，避免脱离上下文删除。 */
export default function Health() {
  const { data, isLoading, isError, error, refetch, isFetching } = useAudit(60)
  const clearSlot = useClearSlot()
  const [confirmSlot, setConfirmSlot] = useState<AuditFinding | null>(null)
  const [isRechecking, setIsRechecking] = useState(false)
  const recheckInFlight = useRef(false)
  const recheckTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      if (recheckTimer.current !== null) clearTimeout(recheckTimer.current)
    }
  }, [])

  const recheck = async () => {
    if (recheckInFlight.current || isFetching) return
    recheckInFlight.current = true
    setIsRechecking(true)
    const startedAt = performance.now()
    try {
      await refetch()
    } catch {
      // 查询错误由 useAudit 呈现；点击事件不再产生额外的未处理拒绝。
    } finally {
      if (!mounted.current) {
        recheckInFlight.current = false
        return
      }
      const finish = () => {
        recheckInFlight.current = false
        recheckTimer.current = null
        setIsRechecking(false)
      }
      // 只延长按钮反馈；查询结果仍按真实请求完成时间显示。
      const remaining = RECHECK_MIN_FEEDBACK_MS - (performance.now() - startedAt)
      if (remaining > 0) recheckTimer.current = setTimeout(finish, remaining)
      else finish()
    }
  }

  if (!data && isLoading) return (
    <div className="flex w-full flex-1 items-center justify-center">
      <Spinner label={t.health.loading} />
    </div>
  )
  if (!data) return isError ? <ErrorState message={(error as Error).message} onRetry={() => { void refetch() }} /> : null

  const kinds = Object.entries(data.by_kind).sort(([a], [b]) => findingPriority(a) - findingPriority(b))
  const findings = [...data.findings].sort((a, b) => findingPriority(a.kind) - findingPriority(b.kind))
  const rechecking = isRechecking || isFetching

  return (
    <div className="space-y-6">
      {isError && <ErrorState message={`体检更新失败，显示上次结果并保留编辑：${(error as Error).message}`} onRetry={() => { void refetch() }} />}
      <PageHeader title={t.health.title} />
      <Card>
        <CardTitle hint={
          <Button variant="ghost" onClick={recheck} disabled={rechecking} aria-busy={rechecking}>
            <RefreshCw className={`h-4 w-4 ${rechecking ? 'animate-spin [animation-duration:1.5s] motion-reduce:animate-none' : ''}`} /> {t.health.recheck}
          </Button>
        }>
          <span className="inline-flex items-center gap-2">
            {data.total_findings
              ? <AlertTriangle className="h-[18px] w-[18px] text-brand-rose" />
              : <ShieldCheck className="h-[18px] w-[18px] text-brand-mint" />}
            {t.health.summary}
          </span>
        </CardTitle>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <span className="text-ghost">{t.health.checked(data.checked.facts, data.checked.entities)}</span>
          <span className={data.total_findings ? 'font-semibold text-brand-amber' : 'font-semibold text-brand-mint'}>
            {data.total_findings ? t.health.foundN(data.total_findings) : t.health.allClear}
          </span>
        </div>
        {!!data.total_findings && (
          <div className="mt-4 flex flex-wrap gap-x-4 gap-y-3">
            {kinds.map(([kind, n]) => (
              <span key={kind} className="inline-flex items-center gap-1.5">
                <FindingLabel kind={kind as AuditFinding['kind']} />
                <span className="text-sm font-normal tabular-nums text-ghost">x{n}</span>
              </span>
            ))}
          </div>
        )}
      </Card>
      {!data.findings.length ? (
        <PageEmptyState title={t.health.allClear} hint={t.health.allClearHint} />
      ) : (
        <Card>
          <CardTitle>{t.health.worthLook}</CardTitle>
          <div className="divide-y divide-line">
            {findings.map((finding, i) => (
              <FindingRow key={`${finding.kind}-${finding.fact_id ?? finding.subject ?? finding.entity ?? i}-${finding.predicate ?? ''}`} finding={finding} onClearSlot={() => setConfirmSlot(finding)} />
            ))}
          </div>
          {data.truncated && <p className="mt-4 text-xs text-ghost">{t.health.truncated}</p>}
        </Card>
      )}
      <ConfirmDialog
        open={!!confirmSlot}
        title={t.health.clearSlotTitle(confirmSlot?.count ?? 0)}
        compact
        danger
        confirmLabel={t.common.delete}
        loading={clearSlot.isPending}
        body={
          <div className="space-y-2">
            <p>{confirmSlot && t.health.clearSlotBody(confirmSlot.count ?? 0, confirmSlot.label ?? '', confirmSlot.predicate ?? '')}</p>
            {!!confirmSlot?.samples?.length && (
              <ul className="space-y-1 rounded-lg border border-line bg-ink-700/40 px-3 py-2">
                {confirmSlot.samples.slice(0, 5).map((sample) => (
                  <li key={sample.fact_id} className={`${FINDING_TEXT_CLASS} text-slate-100`}>{sample.text}</li>
                ))}
              </ul>
            )}
            <p className="text-xs text-ghost">{t.health.clearSlotIrreversible}</p>
          </div>
        }
        onClose={() => setConfirmSlot(null)}
        onConfirm={() => {
          if (!confirmSlot?.subject || !confirmSlot.predicate || !confirmSlot.count) return
          clearSlot.mutate(
            { subject: confirmSlot.subject, predicate: confirmSlot.predicate, count: confirmSlot.count },
            {
              onSuccess: (res) => {
                if (res.ok) toast.success(t.health.clearSlotDone(res.deleted))
                else toast.error(t.health.clearSlotStale)
                setConfirmSlot(null)
              },
              onError: (e) => toast.error((e as Error).message),
            },
          )
        }}
      />
    </div>
  )
}

function FindingRow({ finding: f, onClearSlot }: { finding: AuditFinding; onClearSlot: () => void }) {
  const update = useEditFact()
  const del = useDeleteFact()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [attributeDraft, setAttributeDraft] = useState<AttributeDraft>({ subject: '', predicate: '', object: '' })
  const [handled, setHandled] = useState(false)
  const [savedText, setSavedText] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const editorActions = useRef<HTMLDivElement>(null)
  const saveInFlight = useRef(false)
  const deleteInFlight = useRef(false)
  const detail = t.health.details[f.kind]
  const statement = savedText ?? (f.kind === 'embedder_blind'
    ? t.health.embedderBlindSummary(f.embedder ?? '', typeof f.ratio === 'number' ? Math.round(f.ratio * 100) : null)
    : f.display || f.text || f.entity || '')
  const isOutcome = OUTCOME_PREDICATES.has(f.predicate?.toLowerCase() ?? '')
  const canSave = isOutcome ? !!draft.trim() : Object.values(attributeDraft).every(value => !!value.trim())

  const save = () => {
    if (!f.fact_id || saveInFlight.current || update.isPending || !canSave) return
    const next = draft.trim()
    const fields = {
      subject: attributeDraft.subject.trim(),
      predicate: attributeDraft.predicate.trim(),
      object: attributeDraft.object.trim(),
    }
    if (isOutcome ? next === statement :
      fields.subject === f.subject && fields.predicate === f.predicate && fields.object === f.object) {
      setEditing(false)
      return
    }
    saveInFlight.current = true
    update.mutate({ id: f.fact_id, patch: isOutcome ? { statement: next } : fields }, {
      onSuccess: (res) => {
        toast.success(t.health.fixed)
        setSavedText(res.display || res.text)
        setHandled(true)
        setEditing(false)
      },
      onError: (e) => toast.error((e as Error).message),
      onSettled: () => { saveInFlight.current = false },
    })
  }

  const remove = () => {
    if (!f.fact_id || deleteInFlight.current || del.isPending) return
    deleteInFlight.current = true
    del.mutate(f.fact_id, {
      onSuccess: () => { toast.success(t.health.removed); setHandled(true); setConfirming(false) },
      onError: (e) => toast.error((e as Error).message),
      onSettled: () => { deleteInFlight.current = false },
    })
  }

  return (
    <div className={handled ? 'py-4 opacity-40' : 'py-4'}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <FindingLabel kind={f.kind} />
        {f.valid_at_h && <span className="text-xs text-ghost">{f.valid_at_h}</span>}
        {handled && <span className="inline-flex items-center gap-1 text-xs text-brand-mint"><Check className="h-3.5 w-3.5" /> {t.health.handled}</span>}
      </div>
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          {editing ? (
            isOutcome ? <StatementEditor value={draft} onChange={setDraft} disabled={update.isPending} onBlur={(event) => {
              if (editorActions.current?.contains(event.relatedTarget as Node | null)) return
              save()
            }} onCancel={() => { if (!update.isPending) setEditing(false) }} /> :
              <AttributeEditor value={attributeDraft} onChange={setAttributeDraft} disabled={update.isPending} />
          ) : statement && <p className={`${FINDING_TEXT_CLASS} text-slate-100`}>{statement}</p>}
          {!!f.samples?.length && (
            <ul className="space-y-1">
              {f.samples.slice(0, 5).map((sample) => (
                <li key={sample.fact_id} className={`${FINDING_TEXT_CLASS} text-slate-100`}>{sample.text}</li>
              ))}
            </ul>
          )}
        </div>
        {!f.fact_id && f.kind === 'slot_overflow' && !!f.count && (
          <button type="button" onClick={onClearSlot} className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-xs text-ghost transition hover:border-brand-cyan/50 hover:text-foreground">
            <Eraser className="h-3.5 w-3.5" /> {t.health.clearSlot(f.count)}
          </button>
        )}
        {f.fact_id && !handled && (
          <div ref={editorActions} onMouseDown={(event) => { if (editing) event.preventDefault() }} className="flex shrink-0 items-start gap-3">
            {editing ? (
              <>
                <button type="button" onClick={save} disabled={!canSave || update.isPending} aria-busy={update.isPending} aria-label={t.common.save} title={t.common.save} className="rounded-lg border border-line p-1.5 text-brand-mint transition hover:border-brand-mint/50 disabled:opacity-50">
                  {update.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                </button>
                <button type="button" onClick={() => setEditing(false)} disabled={update.isPending} aria-label={t.common.cancel} title={t.common.cancel} className="rounded-lg border border-line p-1.5 text-ghost transition hover:text-foreground disabled:opacity-50">
                  <X className="h-3.5 w-3.5" />
                </button>
              </>
            ) : (
              <>
                <button type="button" onClick={() => {
                  setDraft(statement)
                  setAttributeDraft({ subject: f.subject ?? '', predicate: f.predicate ?? '', object: f.object ?? '' })
                  setEditing(true)
                }} aria-label={t.health.fix} title={t.health.fix} className="rounded-lg border border-line p-1.5 text-ghost transition hover:border-brand-cyan/50 hover:text-brand-cyan">
                  <Pencil className="h-3.5 w-3.5" />
                </button>
                <button type="button" onClick={() => setConfirming(true)} disabled={del.isPending} aria-label={t.common.delete} title={t.common.delete} className="rounded-lg border border-line p-1.5 text-ghost transition hover:border-brand-rose/50 hover:text-brand-rose disabled:opacity-50">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </>
            )}
          </div>
        )}
      </div>
      <p className="mt-2 text-xs leading-relaxed text-ghost">
        {f.kind === 'slot_overflow' && f.count ? t.health.slotOverflowWhy(f.count, f.label || '', f.predicate || '') : detail?.why ?? f.why}{' '}
        <span className="text-slate-300">{detail?.action ?? f.action}</span>
      </p>
      {f.kind === 'embedder_blind' && f.migrate && (
        <p className="mt-1 break-words text-xs text-ghost">{t.health.embedderCommand}<code className="text-slate-300">{f.migrate}</code></p>
      )}
      <ConfirmDialog
        open={confirming}
        title={t.health.deleteTitle}
        compact
        danger
        confirmLabel={t.common.delete}
        loading={del.isPending}
        body={
          <div className="space-y-3">
            <p>{t.health.deleteBody}</p>
            <p className={`rounded-lg border border-line bg-ink-700/40 px-3 py-2 ${FINDING_TEXT_CLASS} text-slate-100`}>{statement}</p>
          </div>
        }
        onClose={() => { if (!del.isPending && !deleteInFlight.current) setConfirming(false) }}
        onConfirm={remove}
      />
    </div>
  )
}

/** 属性修正显式确认三个字段，切换输入框时不自动保存不完整的修改。 */
function AttributeEditor({ value, onChange, disabled }: {
  value: AttributeDraft
  onChange: (value: AttributeDraft) => void
  disabled: boolean
}) {
  const fields = [
    ['subject', t.facts.subjectLabel],
    ['predicate', t.facts.predicateLabel],
    ['object', t.facts.objectLabel],
  ] as const
  return (
    <div role="group" aria-label={t.health.fix} className="grid gap-2 sm:grid-cols-3">
      {fields.map(([field, label], index) => (
        <label key={field} className="min-w-0 space-y-1 text-xs text-ghost">
          <span>{label}</span>
          <input className="input w-full text-sm" value={value[field]} disabled={disabled} autoFocus={index === 0}
            onChange={(event) => onChange({ ...value, [field]: event.target.value })} />
        </label>
      ))}
    </div>
  )
}

/** 仅会话结论使用整句原位编辑和失焦保存。 */
function StatementEditor({ value, onChange, onBlur, onCancel, disabled }: {
  value: string
  onChange: (value: string) => void
  onBlur: (event: FocusEvent<HTMLTextAreaElement>) => void
  onCancel: () => void
  disabled: boolean
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    ref.current?.focus()
    ref.current?.setSelectionRange(value.length, value.length)
  }, [])
  return (
    <div className={`relative ${FINDING_TEXT_CLASS}`}>
      <div aria-hidden="true" className="invisible">{value + '\u200b'}</div>
      <textarea ref={ref} rows={1} aria-label={t.health.fix} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} onBlur={onBlur} onKeyDown={(e) => { if (e.key === 'Escape' && !e.nativeEvent.isComposing) onCancel() }} className="absolute inset-0 m-0 block h-full min-h-0 w-full resize-none overflow-hidden rounded-md border-0 bg-ink-700/70 p-0 text-slate-100 [font:inherit] whitespace-pre-wrap break-words outline-none ring-1 ring-brand-cyan/40 focus:ring-brand-cyan/60" />
    </div>
  )
}
