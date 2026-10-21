import { useState } from 'react'
import { Outlet, useMatch } from 'react-router-dom'

import { GlobalErrorBanner } from './GlobalErrorBanner'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { Toaster } from './Toast'
import { useQueryErrors } from '../store/queryErrors'

export default function Layout() {
  const [navOpen, setNavOpen] = useState(false)
  const isGraphPage = Boolean(useMatch('/graph'))
  const isTimelinePage = Boolean(useMatch('/timeline'))
  const isProfilePage = Boolean(useMatch('/profile'))
  const isFactsPage = Boolean(useMatch('/facts'))
  const isJournalPage = Boolean(useMatch('/journal'))
  const isHealthPage = Boolean(useMatch('/health'))
  const stretchesContent = isTimelinePage || isProfilePage || isFactsPage || isJournalPage || isHealthPage
  const hasQueryFailure = useQueryErrors((state) => state.failures.length > 0)

  return (
    <div className={isGraphPage ? 'h-dvh overflow-hidden' : 'min-h-screen'}>
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
      <div className={`lg:pl-64 ${isGraphPage ? 'flex h-full min-h-0 flex-col' : stretchesContent ? 'flex min-h-screen flex-col' : ''}`}>
        <Topbar onMenu={() => setNavOpen(true)} />
        <main className={`mx-auto max-w-[1600px] px-4 py-6 sm:px-6 sm:py-8 lg:px-8 ${isGraphPage ? 'min-h-0 w-full flex-1' : stretchesContent ? 'flex w-full flex-1 flex-col' : ''}`}>
          <GlobalErrorBanner />
          {/* 事实页在列表内处理失败；保留筛选栏才能改变范围或重试当前查询。 */}
          {(!hasQueryFailure || isFactsPage) && <Outlet />}
        </main>
      </div>
      <Toaster />
    </div>
  )
}
