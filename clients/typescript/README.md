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

Registry availability depends on the actual published release. The examples below use the package's declared name. Run the HTTP server separately using the [deployment guide](https://github.com/NotZhu/MnemoTrace/blob/main/deploy/README.md).

## Connect and use memory

```ts
import { MnemoTraceClient } from 'mnemotrace-memory'

const client = new MnemoTraceClient({
  baseUrl: 'http://127.0.0.1:8000',
  apiKey: 'YOUR_API_KEY',
})

await client.remember('I live in Paris.', { sessionId: 'demo', scope: 'long' })
const result = await client.recall('Where do I live?', { sessionId: 'demo' })
console.log(result.context)
await client.closeSession('demo')
console.log(await client.sessionReport('demo'))
```

The server maps the Bearer key to an identity and permissions. A namespace or session name is not a credential. For browser use, configure same-origin access or an appropriate gateway; the client does not make cross-origin access policy disappear.

## Constructor

`new MnemoTraceClient({ baseUrl?, apiKey?, fetch?, headers? })`

- baseUrl defaults to `http://localhost:8000`; a trailing slash or `/v1` is normalized.
- apiKey becomes the Authorization Bearer header.
- fetch allows a custom implementation; headers adds request headers.
- The client does not provide an automatic retry or request-timeout policy. Configure those in your application or custom fetch.

## API reference

All operations below return promises. Option names are camelCase except where an imported payload type mirrors the HTTP body.

| Method | Parameters and result |
| --- | --- |
| `health()` | Health; readiness is a separate HTTP endpoint |
| `remember(content, options?)` | sessionId, scope, space; RememberResult |
| `recall(query, options?)` | nChunks=6, sessionId, asOf, redactSensitive=false, spaces; RecallResult |
| `search(query, options?)` | asOf, redactSensitive; SearchResult via lean=false |
| `closeSession(sessionId?, options?)` | default session; summarize/clearWorking default true; CloseSessionResult |
| `sessionReport(sessionId, options?)` | includeSensitive=false; SessionReport |
| `sessions(options?)` | limit, offset, query; SessionIndex |
| `memories(options?)` | pagination and filters described below; MemoryDump |
| `stats()` | MemoryStats without memory text |
| `agentStatus(options?)` | optional sessionId; AgentStatus |
| `profile()` | ProfileResult |
| `addFact(fact)` | predicate/object plus optional subject/sensitive/category/topic; id, text and topic metadata |
| `updateFact(id, patch)` | FactPatch, including topic/reset_topic; id, text and topic metadata |
| `deleteFact(id)` | deletion result; inspect ok |
| `getFocus()` / `setFocus(partial)` | track/mute lists |
| `getPolicy()` / `setPolicy(partial)` | extraction, summary and persona overrides |
| `graph(options?)` | asOf, includeSensitive=false; GraphResponse（nodes、edges、has_more） |
| `import(params)` | ImportParams sent as the HTTP import payload; ImportResult |
| `export(options?)` | includeSensitive=false; MemoryExport by default |
| `forget({ confirm: true })` | irreversible namespace erase; missing confirmation fails before sending |
| `chat.completions.create(params)` | ChatCompletion, or AsyncIterable when stream=true |

MemoryListOptions supports topic, factsLimit/factsOffset, episodesLimit/episodesOffset, status, kind, outcomeKind, query, includeSensitive, expectedRevision, and evaluatedAt. The existing q alias is also accepted by sessions/memories, with query taking precedence. Use query in new callers. Other legacy status aliases remain server-defined; this documentation does not change them.

The optional topic is an exact stored-topic filter, applied before paging; `'unknown'` includes manual and unassigned unknown facts. It intersects with other fact filters and does not filter raw episodes.

For consistent pagination, carry the response's data_revision and evaluated_at into expectedRevision/evaluatedAt on the next request. On a 409 revision change, start again from the first page.

The SDK does not expose every HTTP endpoint or filter: category filtering, document upload, structured profiles, standalone working memory, conflict resolution, slot clearing, and the close request's outcomes override require direct HTTP access where applicable.

## Types and errors

Public types are exported from [types.ts](https://github.com/NotZhu/MnemoTrace/blob/main/clients/typescript/src/types.ts). Core results include RememberResult, RecallResult, SearchResult, Fact, MemoryDump, SessionIndex, SessionReport, ImportResult, MemoryExport, and chat completion types.

`Topic` is a fixed set of main topics, with a separate `TopicSource`. Omitting `topic` or passing null leaves it unspecified; selecting `'unknown'` explicitly records a manual choice. Use `{ reset_topic: true }` to re-evaluate the current content with offline rules, without a model call or a supplied topic. The result is a known topic with source `'rule'`, or `'unknown'/'unset'`. New facts without a manual topic and content edits without a replacement use rules. Topic-only edits preserve fact provenance and history. Attribute and session-outcome extraction can also obtain topics from the existing LLM request; the server's `MNEMOTRACE_LLM_TOPICS=0` disables this enhancement while retaining rules. Existing snapshots are restored without reclassification. Profile preference grouping consumes stored topics; unknown facts belong to the other group, with explicit restriction relations kept as a separate exception. Redacted session report rows omit topic metadata. Structured exports preserve both fields and carry no format version. Imports validate the current fields and constraints before applying any records. See the [topic contract](https://github.com/NotZhu/MnemoTrace/blob/main/docs/architecture/memory.md#主题与来源) for precedence and abstention behavior.

Non-2xx responses throw `MnemoTraceError` with status and the parsed error body in detail. Network failures can remain native fetch errors. A successful HTTP response can still contain queued, degraded, or a rejected business result; inspect the operation's payload.

```ts
import { MnemoTraceError } from 'mnemotrace-memory'

try {
  await client.sessionReport('demo')
} catch (error) {
  if (error instanceof MnemoTraceError) console.error(error.status, error.message)
  else throw error
}
```

## Chat and privacy

The chat surface requires a configured server-side answerer. memory options support recall, remember, session_id, scope, n_chunks, as_of, and redact_sensitive. Its model field does not select an arbitrary backend. stream=true exposes SSE chunks; the current server splits a completed response rather than forwarding live model tokens.

Sensitive filtering is view-specific and not a complete anonymization guarantee. Export defaults to a restricted structured view; includeSensitive=true opts into private text. Deleting a fact or closing a session does not erase all source conversations.

## Development and license

In this directory, run `npm ci`, `npm run typecheck`, and `npm test`. Tests run the build through pretest. Repository-wide development guidance is in [CONTRIBUTING](https://github.com/NotZhu/MnemoTrace/blob/main/CONTRIBUTING.md); HTTP semantics are in the [HTTP reference](https://github.com/NotZhu/MnemoTrace/blob/main/docs/reference/http-api.md).

Source: [NotZhu/MnemoTrace](https://github.com/NotZhu/MnemoTrace).
License: [AGPL-3.0](https://github.com/NotZhu/MnemoTrace/blob/main/LICENSE); see [commercial terms](https://github.com/NotZhu/MnemoTrace/blob/main/COMMERCIAL-LICENSE.md).
