import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { useAudit } from '../hooks/queries'
import { copy as t } from '../lib/copy'
import type { AuditFinding } from '../types'

const RECHECK_MIN_FEEDBACK_MS = 600
export default function Health() {
  const { data, isLoading, isError, error, refetch, isFetching } = useAudit(60)
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

  const kinds = Object.entries(data.by_kind)
  const findings = data.findings
  const rechecking = isRechecking || isFetching

  return (
    <div className="space-y-6">
      {isError && <ErrorState message={`体检更新失败，显示上次结果：${(error as Error).message}`} onRetry={() => { void refetch() }} />}
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
            {findings.map(finding => (
              <FindingRow key={`${finding.kind}-${finding.edge_id ?? finding.fact_id ?? finding.entity_id}`} finding={finding} />
            ))}
          </div>
          {data.truncated && <p className="mt-4 text-xs text-ghost">{t.health.truncated}</p>}
        </Card>
      )}
    </div>
  )
}

function FindingLabel({ kind }: { kind: AuditFinding['kind'] }) {
  return (
    <span className="inline-flex rounded-md bg-brand-amber/15 px-2 py-0.5 text-xs font-medium text-brand-amber">
      {t.health.kind[kind]}
    </span>
  )
}

function FindingRow({ finding }: { finding: AuditFinding }) {
  const detail = t.health.details[finding.kind]
  return (
    <div className="space-y-2 py-4">
      <FindingLabel kind={finding.kind} />
      <p className="text-sm text-slate-100">{detail.why}</p>
      <p className="text-xs text-ghost">{detail.action}</p>
      <div className="flex flex-wrap gap-x-4 text-xs text-ghost">
        {finding.fact_id && <span>事实 <code>{finding.fact_id}</code></span>}
        {finding.edge_id && <span>图边 <code>{finding.edge_id}</code></span>}
        {finding.entity_id && <span>节点 <code>{finding.entity_id}</code></span>}
      </div>
    </div>
  )
}
