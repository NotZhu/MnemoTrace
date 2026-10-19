// 少量展示层辅助函数。客户端不做日期计算 — 服务器已渲染好人类可读的时间戳
// （bi-temporal、UTC），我们只做拆分/格式化和文本截断。

/** 将服务器时间戳（"YYYY-MM-DD HH:MM:SS"，北京时间）拆分为日期和时间两部分。
 *  旧数据/仅日期的时间戳 `time` 为 ''，调用方可选择性地渲染。 */
export function splitStamp(s: string | null | undefined): { date: string; time: string } {
  if (!s) return { date: '', time: '' }
  const i = s.indexOf(' ')
  return i === -1 ? { date: s, time: '' } : { date: s.slice(0, i), time: s.slice(i + 1) }
}

export function compactNumber(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
  return `${(n / 1_000_000).toFixed(1)}M`
}

export function truncate(s: string, max = 140): string {
  if (!s) return ''
  return s.length > max ? `${s.slice(0, max - 1)}…` : s
}
