import { useState } from 'react'
import { Copy, LogOut, Menu } from 'lucide-react'

import { useAuth } from '../store/auth'
import { copy as t } from '../lib/copy'
import { copyText } from '../lib/clipboard'

import { toast } from './Toast'
import { ThemeToggle } from './ThemeToggle'
import { SpacePicker } from './SpacePicker'

function maskKey(key: string | null | undefined, reveal: boolean) {
  if (!key) return '?'
  if (reveal || key.length <= 6) return key
  return `${key.slice(0, 3)}…${key.slice(-3)}`
}

export function Topbar({ onMenu }: { onMenu: () => void }) {
  const apiKey = useAuth((s) => s.apiKey)
  const logout = useAuth((s) => s.logout)
  const [reveal, setReveal] = useState(false)

  const copyKey = async () => {
    if (!apiKey) return
    try {
      await copyText(apiKey)
      toast.success(t.topbar.keyCopied)
    } catch {
      toast.error(t.common.copyFailed)
    }
  }

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-ink">
      <div className="mx-auto flex h-[72px] max-w-[1600px] items-center gap-3 px-4 sm:px-6 lg:px-8">
        <button onClick={onMenu} aria-label="打开导航" className="rounded-lg p-2 text-ghost hover:text-slate-100 lg:hidden">
          <Menu className="h-5 w-5" />
        </button>

        <SpacePicker />
        <div className="flex-1" />

        <div className="flex min-w-0 items-center gap-1.5 rounded-xl border border-line bg-ink-700 px-2 py-1.5">
          <span
            aria-hidden="true"
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[8px] border border-brand-cyan/30 bg-brand-cyan/15 text-xs font-semibold text-brand-cyan"
          >
            {Array.from(apiKey ?? '')[0]?.toUpperCase() || '?'}
          </span>
          <span
            onClick={() => setReveal((v) => !v)}
            title={reveal ? t.topbar.hideKey : t.topbar.showKey}
            className="min-w-0 max-w-[116px] cursor-pointer select-none truncate py-0.5 text-sm text-slate-200 transition hover:text-brand-cyan"
          >
            {maskKey(apiKey, reveal)}
          </span>
          <button
            type="button"
            onClick={copyKey}
            aria-label={t.topbar.copyKey}
            title={t.topbar.copyKey}
            className="rounded-md p-1 text-ghost transition hover:text-slate-100"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        </div>

        <ThemeToggle />

        <button
          onClick={logout}
          className="flex items-center gap-1.5 rounded-xl border border-line bg-ink-800 px-3 py-2 text-sm text-ghost transition hover:border-brand-rose/40 hover:text-brand-rose"
          title={t.topbar.logoutTitle}
        >
          <LogOut className="h-4 w-4" />
          <span className="hidden sm:inline">{t.topbar.logout}</span>
        </button>
      </div>
    </header>
  )
}
