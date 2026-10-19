import { NavLink } from 'react-router-dom'
import {
  BrainCircuit,
  Clock,
  Database,
  HeartPulse,
  LayoutDashboard,
  MessagesSquare,
  NotebookPen,
  Search,
  Share2,
  ShieldCheck,
  SlidersHorizontal,
  Target,
  UserRound,
  X,
} from 'lucide-react'

import { copy as t } from '../lib/copy'
import type { Copy } from '../lib/copy'
import { cx } from './ui'

const NAV: Array<{ to: string; key: keyof Copy['nav']; icon: typeof LayoutDashboard; end?: boolean }> = [
  { to: '/', key: 'dashboard', icon: LayoutDashboard, end: true },
  { to: '/journal', key: 'journal', icon: NotebookPen },
  { to: '/profile', key: 'profile', icon: UserRound },
  { to: '/ask', key: 'ask', icon: Search },
  { to: '/facts', key: 'facts', icon: Database },
  { to: '/health', key: 'health', icon: HeartPulse },
  { to: '/timeline', key: 'timeline', icon: Clock },
  { to: '/graph', key: 'graph', icon: Share2 },
  { to: '/focus', key: 'focus', icon: Target },
  { to: '/conversations', key: 'conversations', icon: MessagesSquare },
  { to: '/settings', key: 'settings', icon: SlidersHorizontal },
  { to: '/privacy', key: 'privacy', icon: ShieldCheck },
]

export function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm lg:hidden" onClick={onClose} />}
      <aside
        className={cx(
          'fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-line bg-ink-800 transition-transform lg:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex items-center justify-between px-5 pb-5 pt-8">
          <Brand />
          <button onClick={onClose} className="rounded-lg p-1.5 text-ghost lg:hidden">
            <X className="h-5 w-5" />
          </button>
        </div>

        <nav className="flex-1 space-y-1 px-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={onClose}
              className={({ isActive }) =>
                cx(
                  'flex items-center gap-3 rounded-xl px-3.5 py-2.5 text-sm font-medium transition',
                  isActive
                    ? 'nav-selected'
                    : 'text-ghost hover:bg-ink-700 hover:text-foreground',
                )
              }
            >
              <item.icon className="h-[18px] w-[18px]" />
              {t.nav[item.key]}
            </NavLink>
          ))}
        </nav>

        <div className="py-4 pl-7 pr-5 text-xs leading-relaxed text-ghost">
          <p>{t.nav.tagline}</p>
        </div>
      </aside>
    </>
  )
}

export function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-[13px] border border-control-line bg-icon-surface text-brand-cyan" aria-hidden="true">
        <BrainCircuit className="h-6 w-6" strokeWidth={1.5} />
      </span>
      <div className="leading-tight">
        <div className="text-base font-medium text-brand-cyan">{t.meta.brand}</div>
        <div className="text-[10px] tracking-[0.18em] text-ghost">{t.nav.consoleSub}</div>
      </div>
    </div>
  )
}
