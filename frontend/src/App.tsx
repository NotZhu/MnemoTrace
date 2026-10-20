import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout'
import { useAuth } from './store/auth'
import { copy as t } from './lib/copy'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Journal from './pages/Journal'
import Profile from './pages/Profile'
import Ask from './pages/Ask'
import Facts from './pages/Facts'
import Health from './pages/Health'
import TimelinePage from './pages/TimelinePage'
import GraphPage from './pages/GraphPage'
import FocusPage from './pages/Focus'
import Conversations from './pages/Conversations'
import Settings from './pages/Settings'
import Privacy from './pages/Privacy'

export default function App() {
  const apiKey = useAuth((s) => s.apiKey)
  const epoch = useAuth((s) => s.epoch)

  // 文档语言固定为中文，旧的语言偏好不参与界面渲染。
  useEffect(() => {
    document.documentElement.lang = 'zh-CN'
    document.title = t.meta.title
  }, [])

  if (!apiKey) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Login />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route element={<Layout key={epoch} />}>
        <Route index element={<Dashboard />} />
        <Route path="journal" element={<Journal />} />
        <Route path="profile" element={<Profile />} />
        <Route path="ask" element={<Ask />} />
        <Route path="facts" element={<Facts />} />
        <Route path="health" element={<Health />} />
        <Route path="timeline" element={<TimelinePage />} />
        <Route path="graph" element={<GraphPage />} />
        <Route path="focus" element={<FocusPage />} />
        <Route path="conversations" element={<Conversations />} />
        <Route path="settings" element={<Settings />} />
        <Route path="privacy" element={<Privacy />} />
      </Route>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
