import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { AlertTriangle, Inbox, Loader2 } from 'lucide-react'

/* ---------- 工具函数 ---------- */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

/* ---------- Button ---------- */
type Variant = 'primary' | 'ghost' | 'danger'
interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  loading?: boolean
}
export function Button({ variant = 'primary', loading, className, children, disabled, ...rest }: ButtonProps) {
  const cls = variant === 'primary' ? 'btn-primary' : variant === 'danger' ? 'btn-danger' : 'btn-ghost'
  return (
    <button className={cx(cls, className)} disabled={disabled || loading} {...rest}>
      {loading && <Loader2 className="h-4 w-4 animate-spin" />}
      {children}
    </button>
  )
}

/* ---------- PageHeader ---------- */
export function PageHeader({
  title,
  actions,
}: {
  title: ReactNode
  actions?: ReactNode
}) {
  // 无操作栏时不占据页面空间，也取消父级 space-y 给下一项添加的顶部间距。
  if (!actions) return <h1 className="sr-only [&+*]:!mt-0">{title}</h1>

  return (
    <div className="mb-6 flex flex-wrap items-center justify-end gap-3">
      {/* 导航已显示页名；保留屏幕阅读器的页面标题，避免视觉上的重复。 */}
      <h1 className="sr-only">{title}</h1>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  )
}

/* ---------- Card ---------- */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <section className={cx('card p-5 sm:p-6', className)}>{children}</section>
}

export function CardTitle({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h3 className="text-sm font-medium tracking-wide text-foreground">{children}</h3>
      {hint && <span className="text-xs text-ghost">{hint}</span>}
    </div>
  )
}

/* ---------- Badge ---------- */
export function Badge({
  children,
  tone = 'default',
}: {
  children: ReactNode
  tone?: 'default' | 'live' | 'old' | 'user' | 'cyan' | 'violet'
}) {
  const tones: Record<string, string> = {
    default: 'bg-ink-700/60 text-ghost',
    live: 'bg-brand-mint/15 text-brand-mint',
    old: 'bg-ink-700/60 text-ghost line-through',
    user: 'bg-brand-amber/15 text-brand-amber',
    cyan: 'bg-brand-cyan/15 text-brand-cyan',
    violet: 'bg-brand-violet/15 text-brand-violet',
  }
  return (
    <span className={cx('inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium', tones[tone])}>
      {children}
    </span>
  )
}

/* ---------- StatCard ---------- */
export function StatCard({
  label,
  value,
  icon,
  accent = 'cyan',
}: {
  label: string
  value: ReactNode
  icon: ReactNode
  accent?: 'cyan' | 'violet' | 'mint' | 'amber'
}) {
  const ring: Record<string, string> = {
    cyan: 'bg-icon-surface text-brand-cyan',
    violet: 'bg-icon-surface text-brand-violet',
    mint: 'bg-icon-surface text-brand-mint',
    amber: 'bg-icon-surface text-brand-amber',
  }
  return (
    <div className="card flex items-center gap-4 p-4">
      <div className={cx('grid h-11 w-11 place-items-center rounded-xl', ring[accent])}>{icon}</div>
      <div>
        <div className="text-2xl font-medium leading-none">{value}</div>
        <div className="mt-1 text-xs text-ghost">{label}</div>
      </div>
    </div>
  )
}

/* ---------- Field ---------- */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      <div className="mt-1.5">{children}</div>
    </label>
  )
}

/* ---------- 状态组件 ---------- */
export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-ghost">
      <Loader2 className="h-5 w-5 animate-spin" />
      {label && <span className="text-sm">{label}</span>}
    </div>
  )
}

export function EmptyState({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-14 text-center">
      <div className="grid h-14 w-14 place-items-center rounded-2xl border border-field-line bg-icon-surface text-brand-cyan">
        <span>{icon ?? <Inbox className="h-6 w-6" />}</span>
      </div>
      <div className="text-sm font-medium text-slate-100">{title}</div>
      {hint && <div className="max-w-md text-xs leading-relaxed text-ghost">{hint}</div>}
    </div>
  )
}

export function ErrorBanner({ message, action }: { message: string; action?: ReactNode }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-brand-rose/30 bg-brand-rose/10 p-4 text-sm text-brand-rose backdrop-blur-md">
      <AlertTriangle className="h-5 w-5 shrink-0" />
      <span className="min-w-0 flex-1">{message}</span>
      {action}
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  // 全局条无法说明哪个区域不可用，局部错误和重试始终保留。
  return <ErrorBanner message={message} action={onRetry && (
    <button type="button" className="shrink-0 rounded-lg border border-brand-rose/30 px-2.5 py-1 text-xs" onClick={onRetry}>
      重新加载
    </button>
  )} />
}
