import { useState } from 'react'
import { Outlet, useMatch } from 'react-router-dom'

import { GlobalErrorBanner } from './GlobalErrorBanner'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { Toaster } from './Toast'

export default function Layout() {
  const [navOpen, setNavOpen] = useState(false)
  const isGraphPage = Boolean(useMatch('/graph'))
  const isTimelinePage = Boolean(useMatch('/timeline'))
  const isProfilePage = Boolean(useMatch('/profile'))
  const isFactsPage = Boolean(useMatch('/facts'))
  const isJournalPage = Boolean(useMatch('/journal'))
  const isHealthPage = Boolean(useMatch('/health'))
  const isAskPage = Boolean(useMatch('/ask'))
  const isPrivacyPage = Boolean(useMatch('/privacy'))
  const showGlobalError = isAskPage || isPrivacyPage
  const stretchesContent = isTimelinePage || isProfilePage || isFactsPage || isJournalPage || isHealthPage

  return (
    <div className={isGraphPage ? 'h-dvh overflow-hidden' : 'min-h-screen'}>
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
      <div className={`lg:pl-64 ${isGraphPage ? 'flex h-full min-h-0 flex-col' : stretchesContent ? 'flex min-h-screen flex-col' : ''}`}>
        <Topbar onMenu={() => setNavOpen(true)} />
        <main className={`mx-auto max-w-[1600px] px-4 py-6 sm:px-6 sm:py-8 lg:px-8 ${isGraphPage ? 'min-h-0 w-full flex-1' : stretchesContent ? 'flex w-full flex-1 flex-col' : ''}`}>
          {showGlobalError && <GlobalErrorBanner />}
          {/* 保留页面的独立错误与草稿；顶部提示不替代卡片内的失败反馈。 */}
          <Outlet />
        </main>
      </div>
      <Toaster />
    </div>
  )
}
