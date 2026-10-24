// Browser network fixture for screenshot tooling that cannot inject fetch faults.
// This is an engineering helper, never imported or served by the product.
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const directory = path.resolve(__dirname, '../../.mnemotrace/issue016-037-browser')
fs.mkdirSync(directory, { recursive: true })
const control = path.join(directory, 'faults.json')
if (!fs.existsSync(control)) fs.writeFileSync(control, '{"rules":[]}')
function ruleFor(url) {
  const config = JSON.parse(fs.readFileSync(control, 'utf8'))
  return config.rules.find(r => r.path === url.pathname && (!r.params || Object.entries(r.params).every(([k, v]) => url.searchParams.get(k) === v)))
}
http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1:8016')
  const rule = request.method === 'GET' && ruleFor(url)
  fs.appendFileSync(path.join(directory, 'requests.jsonl'), JSON.stringify({ date: new Date().toISOString(), path: url.pathname, search: url.search, method: request.method, fault: rule?.mode || null }) + '\n')
  const forward = () => {
    const upstream = http.request({ hostname: '127.0.0.1', port: 8000, path: request.url, method: request.method, headers: request.headers }, result => {
      response.writeHead(result.statusCode, result.headers); result.pipe(response)
    })
    upstream.on('error', () => { if (!response.headersSent) response.writeHead(502); response.end() })
    response.on('close', () => upstream.destroy())
    request.pipe(upstream)
  }
  function apply(mode) {
    if (!mode) { forward(); return }
    if (mode === '503') { response.writeHead(503, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ detail: '合成验收：辅助读取暂时失败' })); return }
    if (mode === 'network') { response.destroy(); return }
    const timer = setInterval(() => {
      const next = ruleFor(url)?.mode
      if (next !== 'hold') { clearInterval(timer); apply(next) }
    }, 50)
    response.on('close', () => clearInterval(timer))
  }
  apply(rule?.mode)
}).listen(8016, '127.0.0.1', () => console.log('Synthetic browser fault proxy: http://127.0.0.1:8016 -> :8000'))
