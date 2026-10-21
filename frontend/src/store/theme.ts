import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type Theme = 'dark' | 'light'

let transitionTimer: ReturnType<typeof setTimeout> | undefined

function applyTheme(theme: Theme, animate = false) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  if (transitionTimer !== undefined) clearTimeout(transitionTimer)
  transitionTimer = undefined
  if (animate && typeof window !== 'undefined' && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    root.dataset.themeTransition = 'true'
    // 颜色在 200ms 内同步完成，再移除临时样式以恢复普通交互过渡。
    transitionTimer = setTimeout(() => {
      delete root.dataset.themeTransition
      transitionTimer = undefined
    }, 240)
  } else {
    delete root.dataset.themeTransition
  }
  root.dataset.theme = theme
  root.style.colorScheme = theme
}

interface ThemeState {
  theme: Theme
  toggle: (animate?: boolean) => void
}

export const useTheme = create<ThemeState>()(
  persist(
    (set, get) => ({
      theme: typeof document !== 'undefined' && document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
      toggle: (animate = true) => {
        const theme = get().theme === 'dark' ? 'light' : 'dark'
        applyTheme(theme, animate)
        set({ theme })
      },
    }),
    {
      name: 'mnemotrace.theme',
      partialize: ({ theme }) => ({ theme }),
      merge: (persisted, current) => {
        const choice = (persisted as { theme?: unknown } | null)?.theme
        return { ...current, theme: choice === 'light' ? 'light' : 'dark' }
      },
    },
  ),
)

applyTheme(useTheme.getState().theme)
