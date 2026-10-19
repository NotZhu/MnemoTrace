# mnemotrace-memory (TypeScript/JavaScript SDK)

Official SDK for [MnemoTrace](https://github.com/NotZhu/MnemoTrace) — the open-source long-term memory engine
for LLM agents. Zero runtime dependencies (uses the global `fetch`); works in Node 18+, browsers, Deno,
Bun, and edge runtimes.

The packages have not been published to npm or PyPI. Build the SDK from this checkout,
then install the local package into your application (replace the path with your checkout):

```bash
# Run from this repository root.
npm --prefix clients/typescript ci
npm --prefix clients/typescript run build
# Run from your application's directory.
npm install /path/to/MnemoTrace/clients/typescript
```

You need a running MnemoTrace server (the SDK talks to its HTTP API). Run from this repository root:

```bash
python -m pip install -e ".[serve]"
export MNEMOTRACE_OPEN=1                 # dev: Bearer text is the namespace; anonymous requires MNEMOTRACE_ALLOW_ANONYMOUS=1
export MNEMOTRACE_EMBEDDER=hashing       # zero-download default; use bge-small for better local embeddings
export MNEMOTRACE_LLM=deepseek           # optional: enables /v1/chat/completions generation
uvicorn mnemotrace.server.app:app --port 8000
```

## Quickstart

```ts
import { MnemoTraceClient } from 'mnemotrace-memory'

const mnemotrace = new MnemoTraceClient({
  baseUrl: 'http://localhost:8000',
  apiKey: 'sk-alice-123', // one key == one isolated memory namespace
})

const sessionId = 'codex:mnemotrace:thread-123'

// content-free wiring check: namespace/session/focus/counts/next actions, no memory text
const status = await mnemotrace.agentStatus({ sessionId })
console.log(status.recommended_next_actions)

// write
await mnemotrace.remember('I live in Shenzhen and work at Tencent.', { sessionId })

// read — a small, dated, relevant slice to ground your own prompt
const { context, tokens_est } = await mnemotrace.recall('where do I live?', { sessionId })

// thread/task ended — finish consolidation, summarize, clear working memory
await mnemotrace.closeSession(sessionId)
const report = await mnemotrace.sessionReport(sessionId) // what this session saved; sensitive facts redacted
const sessions = await mnemotrace.sessions({ limit: 20, query: 'codex' }) // content-free cross-agent index

// or a single direct answer (abstains when not in memory)
const { answer } = await mnemotrace.search('Where do I work?')
```

## OpenAI-compatible chat (drop-in)

MnemoTrace exposes an OpenAI-compatible `/v1/chat/completions` that recalls + injects relevant memory before
the model answers, and remembers the turn afterward. Use the SDK's `chat.completions` surface…

```ts
const completion = await mnemotrace.chat.completions.create({
  model: 'mnemotrace',
  messages: [{ role: 'user', content: 'Remind me where I work.' }],
  memory: { session_id: 'codex:mnemotrace:thread-123', scope: 'auto' },
})
console.log(completion.choices[0].message.content)
console.log(completion.mnemotrace) // { recalled, memory_tokens_est, session_id, remembered }
```

…or keep your existing OpenAI SDK and just change the base URL:

```ts
import OpenAI from 'openai'
const openai = new OpenAI({ baseURL: 'http://localhost:8000/v1', apiKey: 'sk-alice-123' })
await openai.chat.completions.create({ model: 'mnemotrace', messages: [...] }) // now memory-augmented
```

Streaming works too:

```ts
const stream = await mnemotrace.chat.completions.create({
  model: 'mnemotrace',
  messages: [{ role: 'user', content: 'tell me about my projects' }],
  stream: true,
})
for await (const chunk of stream) process.stdout.write(chunk.choices[0]?.delta?.content ?? '')
```

Control the memory layer per request: `memory: { session_id: 'codex:repo:thread' }` to attach the turn to
an agent thread, `{ scope: 'long' }` / `{ scope: 'working' }` for write routing, `{ recall: false }`,
`{ remember: false }`, `{ as_of: 1700864000 }` for a point-in-time memory view, or
`{ redact_sensitive: true }` to omit sensitive facts from injected memory. Redacted contexts are
structured-facts-only: no profile, summaries, or raw chunks.

## Importing an existing history

```ts
import { readFileSync } from 'node:fs'

// ChatGPT export, OpenAI messages, JSONL, or a transcript — the server auto-detects the format.
await mnemotrace.import({ data: JSON.parse(readFileSync('conversations.json', 'utf8')), format: 'chatgpt' })
```

## User-owned export

```ts
// Safe by default: non-sensitive facts + graph; no profile, raw episodes, or summaries.
const portable = await mnemotrace.export()

// Full private export only after an explicit user action.
const fullPrivate = await mnemotrace.export({ includeSensitive: true })
```

## Memory management UI

```ts
// Content-free session index: which Codex / Claude / app sessions touched this namespace.
const sessions = await mnemotrace.sessions({ limit: 20, query: 'claude-code' })

// Share-safe/default view: paginate and search editable non-sensitive facts.
const page = await mnemotrace.memories({
  factsLimit: 50,
  factsOffset: 0,
  episodesLimit: 0,
  status: 'live',
  query: 'work',
})

// Owner-visible inspection view: include raw episodes, profile, and sensitive facts.
const fullPrivatePage = await mnemotrace.memories({
  factsLimit: 50,
  episodesLimit: 10,
  includeSensitive: true,
})
```

## API surface

| Method | HTTP | Returns |
| --- | --- | --- |
| `health()` | GET /health | `Health` readiness + safe deployment introspection |
| `remember(content, { sessionId?, scope? })` | POST /v1/remember | `RememberResult` |
| `recall(query, { nChunks?, sessionId?, asOf?, redactSensitive? })` | POST /v1/recall (lean) | `RecallResult` |
| `closeSession(sessionId, { summarize?, clearWorking? })` | POST /v1/sessions/close | `CloseSessionResult` |
| `sessions({ limit?, offset?, query? })` | GET /v1/sessions | content-free cross-agent/app session index |
| `sessionReport(sessionId, { includeSensitive? })` | GET /v1/sessions/report | `SessionReport` for what this session saved |
| `search(query, { asOf?, redactSensitive? })` | POST /v1/recall | `SearchResult` |
| `memories({ factsLimit?, factsOffset?, episodesLimit?, episodesOffset?, status?, query?, includeSensitive? })` | GET /v1/memories | paged `MemoryDump`; share-safe by default, full owner-visible view with `includeSensitive: true` |
| `agentStatus({ sessionId? })` | GET /v1/agent/status | `AgentStatus` content-free namespace/session/focus/counts/next actions |
| `stats()` | GET /v1/stats | `MemoryStats` content-free namespace observability, including consolidation backlog, hot/cold fact tiers, and page-in/out counts |
| `profile()` | GET /v1/profile | `ProfileResult` |
| `addFact({ subject?, predicate, object })` | POST /v1/facts | `{ ok, id, text }` |
| `updateFact(id, patch)` / `deleteFact(id)` | PATCH/DELETE /v1/facts/:id | — |
| `getFocus()` / `setFocus(f)` | GET/PUT /v1/focus | `Focus` |
| `getPolicy()` / `setPolicy(p)` | GET/PUT /v1/policy | `PolicyResponse` |
| `graph({ asOf?, includeSensitive? })` | GET /v1/graph | `GraphData`; share-safe by default, full owner-visible graph with `includeSensitive: true` |
| `import(params)` | POST /v1/import | `ImportResult` |
| `export({ includeSensitive? })` | GET /v1/export | share-safe structured JSON by default; full private JSON with `includeSensitive: true` |
| `forget({ confirm: true })` | POST /v1/forget | `{ ok, message }`; irreversible namespace wipe |
| `chat.completions.create(params)` | POST /v1/chat/completions | `ChatCompletion` / stream |

Errors throw `MnemoTraceError` with `.status` and a parsed `.detail`.

## Develop

```bash
npm install
npm run build       # dual ESM + CJS + .d.ts into dist/
npm test            # runtime smoke tests against the built dist/
npm run typecheck
```

## License

MnemoTrace is **dual-licensed**: open source under
[GNU AGPL-3.0](https://github.com/NotZhu/MnemoTrace/blob/main/LICENSE), or a separate **commercial
license** for proprietary/closed-source use. Commercial use that won't comply with the AGPL requires
authorization — see
[`COMMERCIAL-LICENSE.md`](https://github.com/NotZhu/MnemoTrace/blob/main/COMMERCIAL-LICENSE.md).
