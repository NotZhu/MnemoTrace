import { create } from 'zustand'
import { isValidApiKey } from '../lib/apiKey'
import { copy as t } from '../lib/copy'
import { useQueryErrors } from './queryErrors'

// 凭证仅存在当前页面内存中；刷新后重新登录。清除此前版本留下的缓存，不读取或迁移旧凭证。
try {
  if (typeof window !== 'undefined') window.localStorage.removeItem('mnemotrace.auth')
} catch { /* 浏览器限制存储访问时，仍可使用内存登录。 */ }

interface AuthState {
  apiKey: string | null
  epoch: number
  keyError: string | null
  login: (key: string) => void
  logout: () => void
}

export const useAuth = create<AuthState>((set) => ({
  apiKey: null,
  epoch: 0,
  keyError: null,
  login: (key) => {
    useQueryErrors.getState().clear()
    const next = isValidApiKey(key.trim())
      ? { apiKey: key.trim(), keyError: null }
      : { apiKey: null, keyError: t.errors.invalidApiKey }
    set(state => ({ ...next, epoch: state.epoch + 1 }))
  },
  logout: () => {
    useQueryErrors.getState().clear()
    set(state => ({ apiKey: null, keyError: null, epoch: state.epoch + 1 }))
  },
}))

/** Read the key outside React (for the fetch client). */
export function currentKey(): string | null {
  return useAuth.getState().apiKey
}

export type AuthSession = Readonly<{ key: string | null; epoch: number }>

export function currentSession(): AuthSession {
  const { apiKey: key, epoch } = useAuth.getState()
  return { key, epoch }
}

export function isCurrentSession(session: AuthSession): boolean {
  const current = currentSession()
  // 同一 Key 再次登录也属于新周期，旧任务不能重新生效。
  return session.epoch === current.epoch && session.key === current.key
}
