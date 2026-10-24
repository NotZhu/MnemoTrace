// Faults affect matching GETs only. Everything else reaches the real local service.
export function installNetwork() {
  const original = window.fetch.bind(window)
  const fixture = window.__consoleValidation = { rules: [], pending: [], requests: [], documentId: crypto.randomUUID() }
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.origin)
    const method = init?.method || (typeof input === 'object' ? input.method : null) || 'GET'
    const rule = method === 'GET' && fixture.rules.find(r => url.pathname === r.path && (!r.params || Object.entries(r.params).every(([k, v]) => url.searchParams.get(k) === v)))
    fixture.requests.push({ path: url.pathname, search: url.search, method, injected: Boolean(rule) })
    if (!rule) return original(input, init)
    return new Promise((resolve, reject) => fixture.pending.push({ path: url.pathname, search: url.search,
      finish(outcome) {
        if (outcome === '503') resolve(new Response(JSON.stringify({ detail: '合成验收：辅助读取暂时失败' }), { status: 503, headers: { 'Content-Type': 'application/json' } }))
        else if (outcome === 'network') reject(new TypeError('Synthetic network failure'))
        else resolve(original(input, init))
      },
    }))
  }
}
