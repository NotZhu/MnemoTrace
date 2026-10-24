// Synthetic fetch boundary for the production UI. Never connects to a memory server.
export function installFixture() {
  const rows = [
    { id: 'synthetic-public', text: 'synthetic public memory', sensitive: false, category: 'public' },
    { id: 'synthetic-private', text: 'synthetic sensitive memory', sensitive: true, category: 'private' },
  ].map(f => ({ ...f, display: f.text, status: 'live', valid_at: '2026-10-21', provenance: [], source: 'extracted', subject: 'synthetic', predicate: 'likes', object: 'synthetic' }))
  const fixture = window.__issue036 = { pending: [], requests: [], hold: false }
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
  window.fetch = async (input) => {
    const url = new URL(String(input), location.origin)
    if (url.pathname === '/health') return json({ ok: true, auth_mode: 'api_keys' })
    if (url.pathname === '/v1/conflicts') return json({ conflicts: [] })
    if (url.pathname !== '/v1/memories') throw new Error(`Unexpected synthetic request: ${url.pathname}`)
    const params = Object.fromEntries(url.searchParams)
    fixture.requests.push(params)
    const respond = (failure = false) => {
      if (failure) return json({ detail: 'synthetic request failed' }, 503)
      const visible = rows.filter(f => params.include_sensitive === 'true' || !f.sensitive)
      const facts = visible.filter(f => (!params.q || f.text.includes(params.q)) && (!params.category || f.category === params.category))
      return json({ user: 'synthetic', facts, episodes: [], fact_categories: visible.map(f => f.category), facts_page: { total: facts.length, has_more: false }, episodes_page: { total: 0, has_more: false } })
    }
    if (fixture.hold || params.include_sensitive === 'false') {
      return new Promise(resolve => fixture.pending.push({ params, finish: failure => resolve(respond(failure)) }))
    }
    return respond()
  }
}
