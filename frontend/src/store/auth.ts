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
  keyError: string | null
  login: (key: string) => void
  logout: () => void
}

export const useAuth = create<AuthState>((set) => ({
  apiKey: null,
  keyError: null,
  login: (key) => {
    useQueryErrors.getState().clear()
    set(isValidApiKey(key.trim())
      ? { apiKey: key.trim(), keyError: null }
      : { apiKey: null, keyError: t.errors.invalidApiKey })
  },
  logout: () => {
    useQueryErrors.getState().clear()
    set({ apiKey: null, keyError: null })
  },
}))

/** Read the key outside React (for the fetch client). */
export function currentKey(): string | null {
  return useAuth.getState().apiKey
}
