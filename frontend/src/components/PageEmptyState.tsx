import { useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode } from 'react'

import { EmptyState } from './ui'

/** 以整个页面内容区为基准居中；上方控件较高时退回正常排列，避免重叠。 */
export function PageEmptyState({ footer, ...props }: ComponentProps<typeof EmptyState> & { footer?: ReactNode }) {
  const container = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const footerRef = useRef<HTMLDivElement>(null)
  const hasFooter = Boolean(footer)
  const [spacing, setSpacing] = useState({ paddingTop: 0, minHeight: 0, paddingBottom: 0 })

  useLayoutEffect(() => {
    const element = container.current
    const inner = content.current
    const main = element?.closest('main')
    if (!element || !inner || !main) return

    const measure = () => {
      const mainStyle = getComputedStyle(main)
      const mainTop = main.getBoundingClientRect().top + window.scrollY
      const top = element.getBoundingClientRect().top + window.scrollY
      const bottomInset = parseFloat(mainStyle.paddingBottom) || 0
      const center = (mainTop + window.innerHeight + (parseFloat(mainStyle.paddingTop) || 0) - bottomInset) / 2
      const next = {
        paddingTop: Math.max(0, center - top - inner.getBoundingClientRect().height / 2),
        minHeight: Math.max(0, window.innerHeight - bottomInset - top),
        // 底部操作单独占位，既不改变空状态的页面中心，也不会把按钮挤出首屏。
        paddingBottom: footerRef.current ? footerRef.current.getBoundingClientRect().height + 24 : 0,
      }
      setSpacing((previous) =>
        previous.paddingTop === next.paddingTop && previous.minHeight === next.minHeight && previous.paddingBottom === next.paddingBottom ? previous : next,
      )
    }

    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(main)
    observer.observe(element)
    observer.observe(inner)
    if (footerRef.current) observer.observe(footerRef.current)
    window.addEventListener('resize', measure)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [hasFooter])

  return (
    <div ref={container} className="relative shrink-0" style={spacing} data-page-empty-state>
      <div ref={content}>
        <EmptyState {...props} />
      </div>
      {hasFooter && <div ref={footerRef} className="absolute inset-x-0 bottom-0 flex justify-center" data-filter-reset-footer>{footer}</div>}
    </div>
  )
}
