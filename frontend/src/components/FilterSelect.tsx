import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Check, ChevronDown } from 'lucide-react'

const appearances = {
  filter: {
    root: 'relative min-w-0 max-w-[180px]',
    trigger: 'flex min-h-[38px] max-w-full items-center gap-2 border-0 bg-transparent p-0 text-sm text-ghost outline-none hover:text-foreground focus-visible:outline-none focus-visible:text-foreground',
    list: 'absolute left-0 top-full z-30 mt-2 max-h-64 w-44 max-w-[calc(100vw-3rem)] overflow-auto rounded-[13px] border border-field-line bg-ink-800 p-1.5 text-sm shadow-card',
    option: 'gap-3 px-2.5 py-2.5', label: 'min-w-0 truncate', icon: 'h-4 w-4',
  },
  scope: {
    root: 'relative min-w-0',
    trigger: 'flex min-h-8 items-center gap-2 rounded-md border-0 bg-transparent p-0 text-left text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand-cyan',
    list: 'absolute left-0 top-full z-30 mt-2 w-32 rounded-[13px] border border-field-line bg-ink-800 p-1.5 text-sm shadow-card',
    option: 'gap-2 px-2 py-2.5', label: undefined, icon: 'h-4 w-4',
  },
  export: {
    root: 'relative w-fit max-w-full',
    trigger: 'inline-flex min-h-8 items-center gap-1 rounded-md border-0 bg-transparent p-0 text-left text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand-cyan disabled:cursor-not-allowed disabled:opacity-50',
    list: 'absolute left-0 top-full z-30 mt-2 w-40 max-w-[calc(100vw-4rem)] rounded-[13px] border border-field-line bg-ink-800 p-1 text-sm shadow-card',
    option: 'gap-2 px-2 py-1.5', label: undefined, icon: 'h-3.5 w-3.5',
  },
}

export function FilterSelect<T extends string>({ label, value, options, onChange,
  disabled = false, describedBy, appearance = 'filter' }: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
  disabled?: boolean
  describedBy?: string
  appearance?: keyof typeof appearances
}) {
  const styles = appearances[appearance]
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const [open, setOpen] = useState(false)
  const selected = Math.max(0, options.findIndex(option => option.value === value))
  const [active, setActive] = useState(selected)
  const activeIndex = Math.min(active, options.length - 1)
  const expanded = open && !disabled
  const labelledBy = appearance === 'scope' ? `${id}-label` : undefined

  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])

  useEffect(() => {
    if (!expanded) return
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', dismiss)
    return () => document.removeEventListener('pointerdown', dismiss)
  }, [expanded])

  useEffect(() => {
    if (expanded) list.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [expanded, activeIndex])

  const choose = (index: number) => {
    if (disabled) return
    if (options[index]) onChange(options[index].value)
    setOpen(false)
  }
  const keyboard = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled || !options.length) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setActive(open ? (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length : selected)
      setOpen(true)
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      setActive(event.key === 'Home' ? 0 : options.length - 1)
      setOpen(true)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (open) choose(activeIndex)
      else { setActive(selected); setOpen(true) }
    } else if (event.key === 'Escape') {
      if (open) { event.preventDefault(); event.stopPropagation(); setOpen(false) }
    } else if (event.key === 'Tab') setOpen(false)
  }

  return (
    <div ref={root} className={styles.root} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
    }}>
      {labelledBy && <span id={labelledBy} className="sr-only">{label}</span>}
      <button type="button" role="combobox" aria-label={labelledBy ? undefined : label}
        aria-labelledby={labelledBy} aria-describedby={describedBy}
        aria-haspopup="listbox" aria-expanded={expanded}
        aria-controls={expanded ? `${id}-list` : undefined}
        aria-activedescendant={expanded ? `${id}-option-${activeIndex}` : undefined}
        disabled={disabled}
        onClick={() => { if (!disabled) { setActive(selected); setOpen(!open) } }} onKeyDown={keyboard}
        className={styles.trigger}>
        <span className={styles.label}>{options[selected]?.label ?? value}</span>
        <ChevronDown aria-hidden="true" className={`${styles.icon} shrink-0 text-ghost transition-transform motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`} />
      </button>
      {expanded && (
        <ul ref={list} id={`${id}-list`} role="listbox" aria-label={labelledBy ? undefined : label}
          aria-labelledby={labelledBy} className={styles.list}>
          {options.map((option, index) => (
            <li key={option.value} id={`${id}-option-${index}`} role="option"
              aria-selected={option.value === value} data-active={activeIndex === index}
              className={`flex cursor-pointer items-center justify-between rounded-lg ${styles.option} ${activeIndex === index ? 'bg-selected text-foreground' : 'text-slate-200'}`}
              onPointerMove={() => setActive(index)} onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(index)}>
              <span className={appearance === 'filter' ? 'min-w-0 break-words' : undefined}>{option.label}</span>
              {option.value === value && <Check aria-hidden="true" className={`${styles.icon} shrink-0 text-brand-cyan`} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
