import { useCallback } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import { sha256 } from '@noble/hashes/sha2.js'
import { currentSession, isCurrentSession, useAuth, type AuthSession } from './auth'
import { apiBaseUrl } from '../lib/api'
import { DEFAULT_QUARTZ_SETTINGS } from '../vendor/quartz/types'
import type { QuartzGraphSettings } from '../vendor/quartz/types'
import { savedTopic } from '../lib/topics'

export const DEFAULT_SPACE_PREFERENCES = {
  graph: { liveOnly: true, hideSensitive: false },
  facts: { status: 'live' as 'live' | 'superseded' | 'all', category: '', topic: '' as ReturnType<typeof savedTopic>, hideSensitive: false },
  journal: { showSuperseded: false, topic: '' as ReturnType<typeof savedTopic> },
  ask: { chunks: 6, redactSensitive: false },
}
type SpacePreferences = typeof DEFAULT_SPACE_PREFERENCES
type Page = keyof SpacePreferences
type Field<P extends Page> = keyof SpacePreferences[P]
const STORAGE_KEY = 'mnemotrace.preferences'
const STABLE_SCOPE = /^space-[a-f0-9]{64}$/
const ranges = { nodeScale: [0.6, 1.8], lineWidth: [0.4, 1.6], repelForce: [0.1, 2.5],
  centerForce: [0, 1], linkDistance: [10, 160], linkForce: [0.1, 1], opacityScale: [1, 6] } as const

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function boolean(value: unknown, fallback: boolean) { return typeof value === 'boolean' ? value : fallback }
function number(value: unknown, min: number, max: number, fallback: number) {
  return typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max ? value : fallback
}
function nextValue<T>(value: SetStateAction<T>, previous: T): T {
  return typeof value === 'function' ? (value as (previous: T) => T)(previous) : value
}
export function validateGraphSettings(value: unknown): QuartzGraphSettings {
  const input = record(value), result = { ...DEFAULT_QUARTZ_SETTINGS }
  for (const key of Object.keys(ranges) as Array<keyof typeof ranges>) {
    const [min, max] = ranges[key]
    result[key] = number(input[key], min, max, result[key])
  }
  result.showRelations = boolean(input.showRelations, result.showRelations)
  return result
}
function validateSpace(value: unknown): SpacePreferences {
  const input = record(value), graph = record(input.graph), facts = record(input.facts)
  const journal = record(input.journal), ask = record(input.ask)
  return {
    graph: { liveOnly: boolean(graph.liveOnly, true), hideSensitive: boolean(graph.hideSensitive, false) },
    facts: {
      status: facts.status === 'live' || facts.status === 'superseded' || facts.status === 'all'
        ? facts.status : 'live',
      category: typeof facts.category === 'string' ? facts.category.trim() : '',
      topic: savedTopic(facts.topic),
      hideSensitive: boolean(facts.hideSensitive, false),
    },
    journal: { showSuperseded: boolean(journal.showSuperseded, false), topic: savedTopic(journal.topic) },
    ask: { chunks: ask.chunks === 1 ? 2 : Number.isInteger(ask.chunks) ? number(ask.chunks, 2, 12, 6) : 6,
      redactSensitive: boolean(ask.redactSensitive, false) },
  }
}
function validateSaved(value: unknown) {
  const saved = record(value), spaces: Record<string, SpacePreferences> = {}
  for (const [id, preferences] of Object.entries(record(saved.spaces))) {
    if (STABLE_SCOPE.test(id)) spaces[id] = validateSpace(preferences)
  }
  return { graph: validateGraphSettings(saved.graph), spaces }
}

let pending: string | undefined, saveTimer: ReturnType<typeof setTimeout> | undefined
export function flushPreferences() {
  if (saveTimer !== undefined) clearTimeout(saveTimer)
  saveTimer = undefined
  if (pending === undefined) return
  try { window.localStorage.setItem(STORAGE_KEY, pending) } catch { /* 本次使用仍保留内存偏好。 */ }
  pending = undefined
}
const storage = createJSONStorage(() => ({
  getItem(name: string) { try { return window.localStorage.getItem(name) } catch { return null } },
  setItem(_name: string, value: string) {
    pending = value
    if (saveTimer !== undefined) clearTimeout(saveTimer)
    saveTimer = setTimeout(flushPreferences, 150)
  },
  removeItem(name: string) { try { window.localStorage.removeItem(name) } catch { /* 存储不可用时忽略。 */ } },
}))

interface PreferencesState {
  graph: QuartzGraphSettings
  spaces: Record<string, SpacePreferences>
  activeScope: string | null
  setGraph(patch: Partial<QuartzGraphSettings>): void
  resetGraph(): void
  setSpace<P extends Page, K extends Field<P>>(page: P, field: K, value: SetStateAction<SpacePreferences[P][K]>): void
}

export const usePreferences = create<PreferencesState>()(persist((set, get) => ({
  graph: { ...DEFAULT_QUARTZ_SETTINGS }, spaces: {}, activeScope: null,
  setGraph(patch) {
    set(state => ({ graph: validateGraphSettings({ ...state.graph, ...patch }) }))
    if (patch.showRelations !== undefined) flushPreferences()
  },
  resetGraph() { set({ graph: { ...DEFAULT_QUARTZ_SETTINGS } }); flushPreferences() },
  setSpace(page, field, value) {
    const session = currentSession()
    const credential = session.key
    if (!credential) return
    const apply = (scope: string) => {
      if (!isCurrentSession(session)) return
      set(state => {
        const current = state.spaces[scope] ?? DEFAULT_SPACE_PREFERENCES
        const next = nextValue(value, current[page][field])
        const updated = validateSpace({ ...current, [page]: { ...current[page], [field]: next } })
        return { spaces: { ...state.spaces, [scope]: updated } }
      })
      flushPreferences()
    }
    const scope = get().activeScope
    if (scope) apply(scope)
    else void preferenceScopeFor(credential).then(apply)
  },
}), {
  name: STORAGE_KEY, version: 2, storage,
  partialize: state => ({ graph: state.graph, spaces: Object.fromEntries(Object.entries(state.spaces).filter(([id]) => STABLE_SCOPE.test(id))) }),
  merge: (saved, current) => ({ ...current, ...validateSaved(saved) }),
  // 旧版本缓存整份重置并回写；保留 migrate 才能让 zustand 覆盖掉磁盘上的旧格式。
  migrate: () => validateSaved(undefined),
}))

// 与现有查询缓存保持同一隔离边界；持久化内容只包含指纹和偏好，不包含凭据。
const scopes = new Map<string, Promise<string>>()
let volatileId = 0
export function preferenceScopeFor(credential: string, server = apiBaseUrl()): Promise<string> {
  const identity = JSON.stringify([server, credential])
  let scope = scopes.get(identity)
  if (!scope) {
    scope = (async () => {
      try {
        const input = new TextEncoder().encode(identity)
        let hash: Uint8Array
        try {
          hash = new Uint8Array(await crypto.subtle.digest('SHA-256', input))
        } catch {
          // 普通 HTTP 等环境没有 subtle；相同输入和算法保留已有空间编号。
          hash = sha256(input)
        }
        return `space-${Array.from(hash, value => value.toString(16).padStart(2, '0')).join('')}`
      } catch { return `volatile-${++volatileId}` }
    })()
    scopes.set(identity, scope)
  }
  return scope
}
function activateScope(session: AuthSession) {
  usePreferences.setState({ activeScope: null })
  const credential = session.key
  if (credential) void preferenceScopeFor(credential).then(activeScope => {
    if (isCurrentSession(session)) usePreferences.setState({ activeScope })
  })
}
activateScope(currentSession())
useAuth.subscribe((state, previous) => {
  if (state.epoch !== previous.epoch) activateScope({ key: state.apiKey, epoch: state.epoch })
})
if (typeof window !== 'undefined') window.addEventListener('pagehide', flushPreferences)

export function useSpacePreference<P extends Page, K extends Field<P>>(page: P, field: K):
  [SpacePreferences[P][K], Dispatch<SetStateAction<SpacePreferences[P][K]>>] {
  const value = usePreferences(state => (state.activeScope ? state.spaces[state.activeScope] : undefined)?.[page][field] ?? DEFAULT_SPACE_PREFERENCES[page][field])
  const update = useCallback((next: SetStateAction<SpacePreferences[P][K]>) => usePreferences.getState().setSpace(page, field, next), [page, field])
  return [value, update]
}
