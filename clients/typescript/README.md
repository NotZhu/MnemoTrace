# mnemotrace-memory (TypeScript/JavaScript SDK)

The SDK for [MnemoTrace](https://github.com/NotZhu/MnemoTrace) uses the global `fetch` and has zero runtime dependencies. It supports Node 18+ and browser environments with `fetch`.

## Install from this checkout

The package has not been published to npm. Build it from this checkout, then install the local package into your application:

```bash
# From the repository root.
npm --prefix clients/typescript ci
npm --prefix clients/typescript run build
# From your application's directory; replace the path with your checkout.
npm install /path/to/MnemoTrace/clients/typescript
```

The SDK requires a running HTTP server. Follow the [deployment guide](https://github.com/NotZhu/MnemoTrace/blob/main/deploy/README.md) for server setup, credentials and model configuration.

## Quickstart

```ts
import { MnemoTraceClient } from 'mnemotrace-memory'

const client = new MnemoTraceClient({
  baseUrl: 'http://localhost:8000',
  apiKey: 'synthetic-key', // replace with a key configured on your server
})
const sessionId = 'app:my-app:thread-123'

const status = await client.agentStatus({ sessionId })
console.log(status.recommended_next_actions)
await client.remember('I work at Acme.', { sessionId, scope: 'long' })
const { context, tokens_est } = await client.recall('Where do I work?', { sessionId })
console.log(context, tokens_est)

await client.closeSession(sessionId)
const report = await client.sessionReport(sessionId)
const sessions = await client.sessions({ limit: 20, query: 'app:my-app' })
```

The server maps keys to authorized spaces. Several keys may share one tenant; a different key does not necessarily mean a different store. Keep a stable `sessionId` through recall, remember and close. Closing consolidates and saves the session; it does not delete it. See the [agent lifecycle guide](https://github.com/NotZhu/MnemoTrace/blob/main/docs/guides/agents.md).

## OpenAI-compatible chat

Chat requires a configured answerer. It recalls memory before answering and saves the user turn afterward; call `closeSession` when the conversation ends.

```ts
const completion = await client.chat.completions.create({
  model: 'mnemotrace',
  messages: [{ role: 'user', content: 'Remind me where I work.' }],
  memory: { session_id: sessionId, scope: 'auto' },
})
console.log(completion.choices[0].message.content)
console.log(completion.mnemotrace)
```

For streaming, iterate over the returned async iterable:

```ts
const stream = await client.chat.completions.create({
  model: 'mnemotrace',
  messages: [{ role: 'user', content: 'Tell me about my projects.' }],
  stream: true,
  memory: { session_id: sessionId },
})
for await (const chunk of stream) {
  console.log(chunk.choices[0]?.delta?.content ?? '')
}
```

The chat `memory` extension uses HTTP snake_case fields. Regular SDK method options use camelCase; responses retain the HTTP field names. Memory controls are documented in the [HTTP reference](https://github.com/NotZhu/MnemoTrace/blob/main/docs/reference/http-api.md#openai-compatible-chat). An existing OpenAI client can use the same endpoint by setting its base URL to `http://localhost:8000/v1`.

## Browse memory consistently

Use `memories` with `factsLimit`/`factsOffset` and `episodesLimit`/`episodesOffset`. The response includes `facts_page`, `episodes_page`, `data_revision` and `evaluated_at`.

Carry the first page's `data_revision` and `evaluated_at` together as `expectedRevision` and `evaluatedAt` on subsequent pages. Advance with `next_offset`. A commit change returns HTTP 409; the SDK preserves the structured error and does not retry automatically. The [complete traversal example](https://github.com/NotZhu/MnemoTrace/blob/main/docs/reference/http-api.md#记忆列表的筛选与一致分页) covers this protocol.

```ts
const page = await client.memories({
  factsLimit: 50,
  episodesLimit: 0,
  kind: 'outcomes',
  outcomeKind: 'lesson',
  status: 'live',
})
```

## Import, export and management

```ts
await client.import({
  data: [{ role: 'user', content: 'I work at Acme.' }],
  format: 'messages',
})

const portable = await client.export() // non-sensitive facts and graph
// Request only when the user explicitly chooses a full private export.
const fullPrivate = await client.export({ includeSensitive: true })
```

Use `search` for a direct factual answer, `addFact`/`updateFact`/`deleteFact` for explicit fact management, and `getFocus`/`setFocus` or `getPolicy`/`setPolicy` for user settings. The `space` option on `remember(content, options)` and `spaces` option on `recall(query, options)` select authorized shared spaces. Full signatures and response types live in [client.ts](https://github.com/NotZhu/MnemoTrace/blob/main/clients/typescript/src/client.ts) and [types.ts](https://github.com/NotZhu/MnemoTrace/blob/main/clients/typescript/src/types.ts).

`health()` calls `/health` for liveness and diagnostics; a successful HTTP response does not prove readiness. Deployment checks use `/ready`. Default browsing, graph and export views omit sensitive content; full private views require `includeSensitive: true`. `redactSensitive: true` on recall restricts context to non-sensitive structured facts, excluding profile, summaries and raw chunks.

Non-success responses throw `MnemoTraceError` with `.status` and the parsed response body in `.detail`. For revision conflicts the machine code is at `error.detail.detail.code`; narrow its type before reading it. Whole-space erasure requires `forget({ confirm: true })` and is irreversible.

## Develop

Run from `clients/typescript/`:

```bash
npm ci
npm run typecheck
npm run build       # ESM, CommonJS and declarations in dist/
npm test            # builds first, then runs runtime regressions
```

## License

The SDK uses the project's AGPL-3.0/commercial dual license. See [LICENSE](https://github.com/NotZhu/MnemoTrace/blob/main/LICENSE) and [COMMERCIAL-LICENSE.md](https://github.com/NotZhu/MnemoTrace/blob/main/COMMERCIAL-LICENSE.md) for the terms.
