import { useState } from 'react'
import { Download, ShieldCheck, Trash2 } from 'lucide-react'

import { Button, Card, CardTitle, PageHeader } from '../components/ui'
import { ConfirmDialog } from '../components/Modal'
import { ExportRangeSelect, type ExportMode } from '../components/ExportRangeSelect'
import { toast } from '../components/Toast'
import { api, StaleSessionError } from '../lib/api'
import { currentSession, isCurrentSession, useAuth } from '../store/auth'
import { useAgentStatus, useForget } from '../hooks/queries'
import { copy as t } from '../lib/copy'

export default function Privacy() {
  const user = useAuth((s) => s.spaceId) ?? 'space'
  const spaceStatus = useAgentStatus()
  const forget = useForget()
  const [exporting, setExporting] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [exportMode, setExportMode] = useState<ExportMode>('compact')
  const excludeSensitive = exportMode !== 'with-originals'
  const protectedNamespace = ['anonymous', 'mnemotrace-test'].includes(user)

  const onExport = async () => {
    const session = currentSession()
    setExporting(true)
    try {
      await api.exportDownload(user.replace(/[^a-zA-Z0-9]/g, '') || 'me', !excludeSensitive)
      if (!isCurrentSession(session)) return
      toast.success(excludeSensitive ? t.privacy.exportedExcl : t.privacy.exported)
    } catch (e) {
      if (e instanceof StaleSessionError || !isCurrentSession(session)) return
      toast.error(String((e as Error).message))
    } finally {
      if (isCurrentSession(session)) setExporting(false)
    }
  }

  return (
    <div className="space-y-6 pt-3">
      <PageHeader title={t.privacy.title} />

      <div className="grid min-w-0 grid-cols-1 gap-8 sm:gap-12">
        <Card>
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Download className="h-4 w-4 text-brand-cyan" /> {t.privacy.exportTitle}
            </span>
          </CardTitle>
          <p className="break-words text-sm leading-relaxed text-ghost">{t.privacy.exportDesc(spaceStatus.data?.space_id)}</p>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-4">
            <div className="min-w-0 flex-1 basis-64">
              <ExportRangeSelect value={exportMode} onChange={setExportMode} disabled={exporting} />
              <p id="privacy-export-mode-hint" className="mt-2 text-xs leading-relaxed text-ghost">
                {excludeSensitive ? t.privacy.exportCompactHint : t.privacy.exportWithOriginalsHint}
              </p>
            </div>
            <Button className="ml-auto" onClick={onExport} loading={exporting}>
              <Download className="h-4 w-4" /> {t.privacy.exportBtn}
            </Button>
          </div>
        </Card>

        <Card>
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Trash2 className="h-4 w-4 text-brand-rose" /> {t.privacy.forgetTitle}
            </span>
          </CardTitle>
          <p className="text-sm leading-relaxed text-ghost">{t.privacy.forgetDesc}</p>
          {protectedNamespace && (
            <p className="mt-3 rounded-lg border border-brand-amber/30 bg-brand-amber/10 px-3 py-2 text-sm text-brand-amber">
              {t.privacy.protectedDemo}
            </p>
          )}
          <div className="mt-4 flex justify-end">
            <Button variant="danger" disabled={protectedNamespace} onClick={() => setConfirm(true)}>
              <Trash2 className="h-4 w-4" /> {t.privacy.forgetBtn}
            </Button>
          </div>
        </Card>
      </div>

      <div className="flex items-start gap-2 text-xs leading-relaxed text-ghost">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand-mint" />
        <div className="space-y-1">
          <p>{t.privacy.selfHostBody}</p>
          <p>{t.privacy.modelProcessingBody}</p>
        </div>
      </div>

      <ConfirmDialog
        open={confirm}
        title={t.privacy.confirmTitle(user)}
        danger
        confirmLabel={t.privacy.confirmLabel}
        loading={forget.isPending}
        confirmDisabled={confirmText !== user}
        body={
          <div className="space-y-4">
            <p>
              {t.privacy.confirmBodyPre}
              <strong className="text-brand-rose">{t.privacy.confirmBodyEmph}</strong>
              {t.privacy.confirmBodyPost}
            </p>
            <label className="block">
              <span className="label">{t.privacy.confirmTypeLabel}</span>
              <input
                className="input mt-1.5"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder={t.privacy.confirmTypePlaceholder}
              />
            </label>
          </div>
        }
        onClose={() => {
          setConfirm(false)
          setConfirmText('')
        }}
        onConfirm={() =>
          forget.mutate(undefined, {
            onSuccess: () => {
              toast.success(t.privacy.forgotten)
              setConfirm(false)
              setConfirmText('')
            },
            onError: (e) => toast.error(String((e as Error).message)),
          })
        }
      />
    </div>
  )
}
