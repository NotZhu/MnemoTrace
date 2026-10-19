import { useEffect, useState } from 'react'

export function useDebouncedSearch(value: string, composing: boolean, delay = 250) {
  const [query, setQuery] = useState(value)
  useEffect(() => {
    // 输入法组词期间的拼音不是用户最终的搜索词。
    if (composing) return
    if (!value) {
      setQuery('')
      return
    }
    const timer = window.setTimeout(() => setQuery(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, composing, delay])
  return query
}
