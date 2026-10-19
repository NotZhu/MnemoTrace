import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Check, ChevronDown, Loader2, Plus } from 'lucide-react'

import { useSessions } from '../hooks/queries'
import { copy as t } from '../lib/copy'

export function SessionPicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [draft, setDraft] = useState('')
  const [composing, setComposing] = useState(false)
  // 独立获取完整会话索引，不能随下方列表的搜索词缩减候选范围。
  const sessions = useSessions({})
  const options = [...new Set(['default', value, ...(sessions.data?.sessions.map((session) => session.id) ?? [])])]
  const selected = options.indexOf(value)

  useEffect(() => {
    if (!open) return
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) { setOpen(false); setDraft(''); setComposing(false) }
    }
    document.addEventListener('pointerdown', dismiss)
    return () => document.removeEventListener('pointerdown', dismiss)
  }, [open])

  const choose = (index: number) => {
    if (options[index]) onChange(options[index])
    setOpen(false)
    setDraft('')
    setComposing(false)
    trigger.current?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Escape') {
      if (open) { event.preventDefault(); event.stopPropagation(); setOpen(false); setDraft('') }
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setActive(open ? (active + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length : selected)
      setOpen(true)
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      setActive(event.key === 'Home' ? 0 : options.length - 1)
      setOpen(true)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (open) choose(active)
      else { setActive(selected); setOpen(true) }
    }
  }

  return (
    <div ref={root} className="relative min-w-0 max-w-full" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) { setOpen(false); setDraft(''); setComposing(false) }
    }}>
      <button ref={trigger} type="button" role="combobox" aria-label={t.conversations.sessionLabel}
        aria-haspopup="listbox" aria-expanded={open}
        aria-controls={open ? `${id}-list` : undefined}
        aria-activedescendant={open && options[active] ? `${id}-option-${active}` : undefined}
        className="flex min-h-8 max-w-full items-center gap-2 rounded-md border-0 bg-transparent p-0 text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand-cyan"
        onClick={() => { setActive(selected); setDraft(''); setComposing(false); setOpen(!open) }} onKeyDown={onKeyDown}>
        <span className="shrink-0 text-ghost">{t.conversations.sessionPickerLabel}</span>
        <span className="min-w-0 truncate">{value}</span>
        <ChevronDown aria-hidden="true" className={`h-4 w-4 shrink-0 text-ghost transition-transform motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-2 w-56 max-w-[calc(100vw-5rem)] rounded-[13px] border border-field-line bg-ink-800 p-1.5 text-sm shadow-card">
          <ul id={`${id}-list`} role="listbox" aria-label={t.conversations.sessionLabel} className="max-h-48 overflow-y-auto">
          {options.map((option, index) => (
            <li key={option} id={`${id}-option-${index}`} role="option" aria-selected={option === value}
              className={`flex h-10 cursor-pointer items-center justify-between gap-2 rounded-lg px-2 ${active === index ? 'bg-selected text-foreground' : 'text-slate-200'}`}
              onPointerMove={() => setActive(index)} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(index)}>
              <span className="min-w-0 truncate" title={option}>{option}</span>
              {option === value && <Check aria-hidden="true" className="h-4 w-4 shrink-0 text-brand-cyan" />}
            </li>
          ))}
          {sessions.isFetching && <li role="none" className="flex items-center gap-2 px-2 py-2 text-xs text-ghost"><Loader2 aria-hidden="true" className="h-3 w-3 animate-spin" />{t.conversations.sessionsLoading}</li>}
          {sessions.isError && <li role="none" className="px-2 py-2 text-xs text-ghost">{t.conversations.sessionPickerError}</li>}
          </ul>
          <form className="mt-1.5 border-t border-line pt-1.5" onSubmit={(event) => {
            event.preventDefault()
            const next = draft.trim()
            if (!next || composing) return
            onChange(next)
            setDraft('')
            setOpen(false)
            trigger.current?.focus()
          }}>
            <div className="relative w-full">
            <input className="input h-10 w-full rounded-lg py-2 pl-2 pr-9 text-sm focus:ring-inset"
              aria-label={t.conversations.newSessionId}
              placeholder={t.conversations.newSessionPlaceholder}
              autoComplete="off" autoCapitalize="none" spellCheck={false} value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.nativeEvent.isComposing || composing)) event.preventDefault()
                if (event.key === 'Escape') {
                  event.preventDefault(); event.stopPropagation(); setDraft(''); setOpen(false); setComposing(false); trigger.current?.focus()
                }
              }} />
            <button type="submit" disabled={!draft.trim() || composing}
              aria-label={t.conversations.useNewSessionId} title={t.conversations.useNewSessionId}
              className="absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md border-0 bg-transparent text-foreground hover:bg-selected disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-cyan">
              <Plus aria-hidden="true" className="h-4 w-4" />
            </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
