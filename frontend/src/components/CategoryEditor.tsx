import { CATEGORY_LABELS, CATEGORY_OPTIONS, CATEGORY_SOURCE_LABELS } from '../lib/categories'
import type { Category, FactMetadata } from '../types'

type CategorySelection = Pick<FactMetadata, 'categories' | 'categories_source'>

export function CategoryBadges({ categories, categories_source }: CategorySelection) {
  if (!categories.length) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs text-ghost"
      title={CATEGORY_SOURCE_LABELS[categories_source]}>
      {categories.map(category => (
        <span key={category} className="rounded-md bg-selected px-1.5 py-0.5 text-brand-cyan">
          {CATEGORY_LABELS[category]}
        </span>
      ))}
      <span>{CATEGORY_SOURCE_LABELS[categories_source]}</span>
    </span>
  )
}

function CategoryChoices({ value, onChange }: { value: Category[]; onChange: (value: Category[]) => void }) {
  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-2">
      {CATEGORY_OPTIONS.map(([category, label]) => (
        <label key={category} className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={value.includes(category)}
            onChange={event => onChange(event.target.checked ? [...value, category] : value.filter(item => item !== category))} />
          {label}
        </label>
      ))}
    </div>
  )
}

export function CategoryFilter({ value, onChange }: { value: Category[]; onChange: (value: Category[]) => void }) {
  return (
    <details className="relative text-sm">
      <summary className="cursor-pointer rounded-lg px-2 py-2 text-ghost">
        {value.length ? `标签（${value.length}）` : '全部标签'}
      </summary>
      <div className="absolute right-0 z-10 w-56 rounded-lg border border-control-line bg-background p-3 shadow-lg">
        <CategoryChoices value={value} onChange={onChange} />
        {value.length > 0 && <button type="button" className="mt-3 text-brand-cyan" onClick={() => onChange([])}>清除筛选</button>}
      </div>
    </details>
  )
}
