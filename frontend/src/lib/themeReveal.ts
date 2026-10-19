import { flushSync } from 'react-dom'
import { useTheme } from '../store/theme'

// react-router-dom 通过全局增强给 document.startViewTransition 增加了重载，
// 它返回的 ViewTransition 与 lib.dom 的同名接口不是同一个类型（缺少 TS 5.9 新增的 types），
// 这里只按实际用到的 skipTransition 做结构声明，避免两个同名声明互相冲突。
let active: { transition: { skipTransition(): void }; animation?: Animation } | undefined

export function revealThemeFrom(button: HTMLButtonElement) {
  const root = document.documentElement
  const toggle = useTheme.getState().toggle
  if (!document.startViewTransition || !root.animate || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    toggle()
    return
  }

  active?.transition.skipTransition()
  active?.animation?.cancel()

  const rect = button.getBoundingClientRect()
  const x = rect.left + rect.width / 2
  const y = rect.top + rect.height / 2
  const radius = Math.ceil(Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y)))
  let applied = false
  root.dataset.themeReveal = 'true'

  try {
    const transition = document.startViewTransition(() => {
      // 新画面快照必须同时包含主题色和 React 更新后的开关状态。
      flushSync(() => toggle(false))
      applied = true
    })
    const current = { transition, animation: undefined as Animation | undefined }
    active = current

    transition.ready.then(() => {
      if (active !== current) return
      current.animation = root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 480, easing: 'cubic-bezier(0.22, 0.8, 0.25, 1)', pseudoElement: '::view-transition-new(root)' },
      )
    }).catch(() => transition.skipTransition())

    const cleanup = () => {
      // 连续点击时，旧动画结束不能清掉新动画使用的样式。
      if (active !== current) return
      delete root.dataset.themeReveal
      active = undefined
    }
    transition.finished.then(cleanup, cleanup)
    transition.updateCallbackDone.catch(() => {})
  } catch {
    delete root.dataset.themeReveal
    active = undefined
    if (!applied) toggle()
  }
}
