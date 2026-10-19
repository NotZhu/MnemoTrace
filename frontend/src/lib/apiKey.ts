// Bearer 凭证原样传输，不编码或替换字符，避免改变鉴权与空间归属。
export function isValidApiKey(key: unknown): key is string {
  return typeof key === 'string' && key.trim().length > 0 && /^[\x20-\x7e]+$/.test(key)
}
