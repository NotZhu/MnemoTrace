import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

import { copy as t } from '../lib/copy'
import { Button } from './ui'

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  compact = false,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  children: ReactNode
  footer?: ReactNode
  compact?: boolean
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className={`card relative z-10 w-full max-w-lg animate-fade-up ${compact ? 'p-5 pb-4 sm:p-6 sm:pb-4' : 'p-5 sm:p-6'}`}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-base font-semibold">{title}</h3>
          <button onClick={onClose} aria-label={t.common.cancel} className="rounded-lg p-1.5 text-ghost transition hover:text-slate-100">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div>{children}</div>
        {footer && <div className={`${compact ? 'mt-4' : 'mt-6'} flex justify-end gap-2`}>{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  body,
  confirmLabel,
  danger = false,
  loading = false,
  confirmDisabled = false,
  compact = false,
}: {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  body: ReactNode
  confirmLabel?: string
  danger?: boolean
  loading?: boolean
  confirmDisabled?: boolean
  compact?: boolean
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      compact={compact}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} loading={loading} disabled={confirmDisabled} onClick={onConfirm}>
            {confirmLabel ?? t.common.confirm}
          </Button>
        </>
      }
    >
      <div className="text-sm leading-relaxed text-slate-300">{body}</div>
    </Modal>
  )
}
