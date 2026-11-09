import { FilterSelect } from './FilterSelect'
import { copy as t } from '../lib/copy'
import type { RememberScope } from '../types'

const options: { value: RememberScope; label: string }[] = [
  { value: 'auto', label: t.conversations.scopeAuto },
  { value: 'long', label: t.conversations.scopeLong },
]

export function ScopeSelect({ value, onChange }: { value: RememberScope; onChange: (value: RememberScope) => void }) {
  return <FilterSelect label={t.conversations.scopeLabel} value={value} options={options}
    onChange={onChange} appearance="scope" />
}
