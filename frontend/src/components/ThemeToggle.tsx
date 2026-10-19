import { Moon, Sun } from 'lucide-react'
import { useTheme } from '../store/theme'
import { copy as t } from '../lib/copy'
import { revealThemeFrom } from '../lib/themeReveal'
import { cx } from './ui'

export function ThemeToggle({ className, reveal = false }: { className?: string; reveal?: boolean }) {
  const theme = useTheme((s) => s.theme)
  const toggle = useTheme((s) => s.toggle)
  const label = theme === 'dark' ? t.theme.switchToLight : t.theme.switchToDark
  return (
    <button
      type="button"
      onClick={(event) => reveal ? revealThemeFrom(event.currentTarget) : toggle()}
      role="switch"
      aria-label="深色模式"
      title={label}
      aria-checked={theme === 'dark'}
      className={cx('inline-flex h-9 w-[72px] shrink-0 rounded-full border border-line bg-secondary p-[3px] text-ghost transition-colors hover:border-control-line', className)}
    >
      {/* 滑块宽度与图标格相同，移动一格加间距；浏览器缩放时仍共用中心。 */}
      <span className="pointer-events-none relative grid h-full w-full grid-cols-2 items-center gap-2">
        <span
          aria-hidden="true"
          className={cx('theme-toggle-thumb absolute left-0 top-1/2 aspect-square w-[calc((100%_-_8px)/2)] -translate-y-1/2 rounded-full bg-ink-800 shadow-[0_1px_4px_rgb(0_0_0/12%)] transition-transform duration-200 motion-reduce:transition-none', theme === 'dark' ? 'translate-x-[calc(100%_+_8px)]' : 'translate-x-0')}
        />
        <span className={cx('relative flex h-full items-center justify-center', theme === 'light' && 'text-foreground')}>
          <Sun className="h-4 w-4" aria-hidden="true" />
        </span>
        <span className={cx('relative flex h-full items-center justify-center', theme === 'dark' && 'text-foreground')}>
          <Moon className="h-4 w-4" aria-hidden="true" />
        </span>
      </span>
    </button>
  )
}
