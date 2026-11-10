# mnemotrace-memory (TypeScript/JavaScript SDK)

TypeScript/JavaScript client for a running MnemoTrace HTTP service. Uses global `fetch` in Node.js 18+ or browsers, with no runtime dependencies. The package exposes ESM, CommonJS, and TypeScript declarations.

## Install from this repository

From the repository root:

```sh
npm --prefix clients/typescript ci
npm --prefix clients/typescript run build
```

In a consuming application, install the built package using its absolute directory:

```sh
npm install /absolute/path/to/MnemoTrace/clients/typescript
```

The examples below use the package's declared name. Follow the [deployment guide](https://github.com/NotZhu/MnemoTrace/blob/main/deploy/README.md) to initialize Postgres, start the service and issue a Bearer key. The memory extraction example requires a configured server LLM.

## Connect and use memory

```ts
import { MnemoTraceClient } from 'mnemotrace-memory'

const client = new MnemoTraceClient({
  baseUrl: 'http://127.0.0.1:8000',
  apiKey: 'YOUR_API_KEY',
})

const { space_id: spaceId } = await client.createSpace('Research')
const scope = { spaceId }
const accepted = await client.rememberMessages([
  { message_id: 'm1', role: 'user', content: 'I live in Paris.' },
], { ...scope, sessionId: 'demo' })
await client.flushProcessing({ ...scope, sessionId: 'demo' })
for (const operationId of accepted.operation_ids) {
  console.log(await client.getOperation(operationId, scope))
}
const result = await client.recall('Where do I live?', { ...scope, sessionId: 'demo' })
console.log(result.context)
```

The server resolves the Bearer key to the account and checks each requested space.
Use `listSpaces()` to select an existing space. Keep the returned ID when renaming the space.

## Constructor

`new MnemoTraceClient({ baseUrl?, apiKey?, fetch?, headers? })`

- baseUrl defaults to `http://localhost:8000`; a trailing slash or `/v1` is normalized.
- apiKey becomes the Authorization Bearer header.
- fetch allows a custom implementation; headers adds request headers.
- The client does not provide an automatic retry or request-timeout policy. Configure those in your application or custom fetch.

## API reference

All methods return promises. Pass `{ spaceId }` to single-space methods. For multi-space recall pass `{ spaceIds }`. Chat uses `memory.space_id`.

| Method | Parameters and result |
| --- | --- |
| `health()` | Health; readiness is a separate HTTP endpoint |
| `remember(content, options)` | sessionId, scope, agentId; RememberResult |
| `rememberMessages(messages, options)` | required sessionId; mode, processing, scope, agentId; MessagesResult |
| `recall(query, options)` | nChunks=6, sessionId, asOf, redactSensitive=false, spaceIds; RecallResult |
| `search(query, options)` | asOf, redactSensitive; SearchResult via lean=false |
| `listSpaces(options?)` / `createSpace(name)` | list or create spaces owned by the authenticated account |
| `getOperation(id, options)` / `retryOperation(id, options)` | query or retry a stable operation ID |
| `flushProcessing(options)` | wait for core processing, with optional sessionId and includeDigests |
| `sessionReport(sessionId, options)` | includeSensitive=false, limit=100, offset=0. Use facts_page.next_offset for subsequent pages |
| `sessions(options)` | limit, offset, query; SessionIndex |
| `memories(options)` | pagination and filters described below; MemoryDump |
| `stats(options)` | MemoryStats without memory text |
| `agentStatus(options)` | optional sessionId; AgentStatus |
| `profile(options)` | ProfileResult |
| `addFact(fact, options)` | predicate/object plus optional subject, sensitivity, categories and object kind; returns id, text and fact metadata |
| `updateFact(id, patch, options)` | FactPatch, including categories and object kind; returns id, text and fact metadata |
| `deleteFact(id, options)` | deletion result; inspect ok |
| `getFocus(options)` / `setFocus(partial, options)` | track/mute lists |
| `getPolicy(options)` / `setPolicy(partial, options)` | extraction, digest and persona overrides |
| `graph(options)` | asOf, includeSensitive=false; GraphResponse（nodes、edges、has_more） |
| `import(params, options)` | ImportParams sent as the HTTP import payload; ImportResult |
| `export(options)` | includeSensitive=false; MemoryExport by default |
| `forget({ spaceId, confirm: true })` | irreversible space clearing; missing confirmation fails before sending |
| `chat.completions.create(params)` | ChatCompletion, or AsyncIterable when stream=true |

MemoryListOptions supports categories, factsLimit/factsOffset, episodesLimit/episodesOffset, status, kind, outcomeKind, query, includeSensitive, expectedRevision, and evaluatedAt. The existing q alias is also accepted by sessions/memories, with query taking precedence. Use query in new callers. Other legacy status aliases remain server-defined; this documentation does not change them.

The optional `categories` array matches any of the supplied labels before pagination. It intersects with other fact filters and does not filter raw episodes.

For consistent pagination, carry the response's data_revision and evaluated_at into expectedRevision/evaluatedAt on the next request. On a 409 revision change, start again from the first page.

Use the HTTP API directly for document uploads, structured profiles, conflict resolution, and slot clearing.

## Types and errors

Public types are exported from [types.ts](https://github.com/NotZhu/MnemoTrace/blob/main/clients/typescript/src/types.ts). Core results include RememberResult, RecallResult, SearchResult, Fact, MemoryDump, SessionIndex, SessionReport, ImportResult, MemoryExport, and chat completion types.

`Category` defines the fixed label catalog. Fact results return `categories`, server-assigned `categories_source`, and `object_kind`. Use `{ categories: [] }` to clear the labels. Metadata-only edits preserve provenance and history. Redacted session report rows omit these fields. Structured exports carry `format_version: 2`. See the [classification contract](https://github.com/NotZhu/MnemoTrace/blob/main/docs/architecture/memory.md#分类与对象类型) for edit semantics.

Non-2xx responses throw `MnemoTraceError` with status and the parsed error body in detail. Network failures can remain native fetch errors. A successful HTTP response can contain a queued acknowledgement or a rejected business result; inspect the operation's payload. Extraction failures return HTTP 502/503 with a stable error code, `stored_raw`, and `episode_ids`; they are not successful degraded results.

```ts
import { MnemoTraceError } from 'mnemotrace-memory'

try {
  await client.sessionReport('demo', { spaceId })
} catch (error) {
  if (error instanceof MnemoTraceError) console.error(error.status, error.message)
  else throw error
}
```

## Chat and privacy

Chat uses the configured server LLM. The memory object requires space_id and supports recall, remember, session_id, scope, n_chunks, as_of, and redact_sensitive. Its model field does not select an arbitrary backend. stream=true exposes SSE chunks; the current server splits a completed response rather than forwarding live model tokens.

Export defaults to a restricted structured view. Set `includeSensitive: true` to include private text. Fact deletion preserves its original sources, which can be erased by clearing or deleting the space.

## Development and license

In this directory, run `npm ci`, `npm run typecheck`, and `npm test`. Tests run the build through pretest. Repository-wide development guidance is in [CONTRIBUTING](https://github.com/NotZhu/MnemoTrace/blob/main/CONTRIBUTING.md); HTTP semantics are in the [HTTP reference](https://github.com/NotZhu/MnemoTrace/blob/main/docs/reference/http-api.md).

Source: [NotZhu/MnemoTrace](https://github.com/NotZhu/MnemoTrace).
License: [AGPL-3.0](https://github.com/NotZhu/MnemoTrace/blob/main/LICENSE); see [commercial terms](https://github.com/NotZhu/MnemoTrace/blob/main/COMMERCIAL-LICENSE.md).


`getEpisode(episodeId, { spaceId })` reads the complete original event referenced by recall. Recall uses the event digest when available. `flushProcessing({ spaceId, includeDigests: true })` also waits for eligible digests. Inspect the core operation's `auxiliary_operation_ids` to look up individual digest results.
