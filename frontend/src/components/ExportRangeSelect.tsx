import { FilterSelect } from './FilterSelect'
import { copy as t } from '../lib/copy'

export type ExportMode = 'compact' | 'with-originals'

const options: { value: ExportMode; label: string }[] = [
  { value: 'compact', label: t.privacy.exportCompact },
  { value: 'with-originals', label: t.privacy.exportWithOriginals },
]

export function ExportRangeSelect({ value, onChange, disabled = false }: {
  value: ExportMode
  onChange: (value: ExportMode) => void
  disabled?: boolean
}) {
  return <FilterSelect label={t.privacy.exportModeLabel} value={value} options={options}
    onChange={onChange} disabled={disabled} describedBy="privacy-export-mode-hint" appearance="export" />
}
