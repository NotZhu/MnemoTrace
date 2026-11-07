// Start the Postgres-backed HTTP service as described in deploy/README.md.
// Set MNEMOTRACE_API_KEY to an issued key and MNEMOTRACE_SPACE_ID to a space you own.
// From clients/typescript, run npm run build and then node examples/node.mjs.
//
// (After `npm run build`, import from 'mnemotrace-memory'. Here we import the built dist directly.)
import { MnemoTraceClient } from '../dist/index.js'

const apiKey = process.env.MNEMOTRACE_API_KEY
const spaceId = process.env.MNEMOTRACE_SPACE_ID
if (!apiKey || !spaceId) {
  throw new Error('Set MNEMOTRACE_API_KEY and MNEMOTRACE_SPACE_ID before running this example')
}

const mnemotrace = new MnemoTraceClient({
  baseUrl: process.env.MNEMOTRACE_URL ?? 'http://localhost:8000',
  apiKey,
})

const sessionId = process.env.MNEMOTRACE_SESSION_ID ?? 'sdk-demo:node'
const scope = { spaceId }
const session = { ...scope, sessionId }

const status = await mnemotrace.agentStatus(session)
console.log('--- agent memory status ---')
console.log({
  space: status.space_id,
  session: status.session,
  focus: status.focus,
  facts: status.counts.facts_live,
})

const memoryScope = status.llm_configured ? 'long' : 'raw'
const accepted = await mnemotrace.rememberMessages([
  { message_id: `node-demo:${memoryScope}:1`, role: 'user', content: 'I live in Shenzhen.' },
  { message_id: `node-demo:${memoryScope}:2`, role: 'user', content: 'I work at Acme.' },
], { ...session, scope: memoryScope, processing: 'buffered' })
console.log('\naccepted:', accepted.operation_ids)
if (accepted.operation_ids.length) {
  const flushed = await mnemotrace.flushProcessing(session)
  console.log('core processing:', flushed.status)
  const operations = await Promise.all(accepted.operation_ids.map((id) => mnemotrace.getOperation(id, scope)))
  console.log('operations:', operations)
  if (!flushed.ok) process.exitCode = 1
}

const { context } = await mnemotrace.recall('where do I work and live?', session)
console.log('--- recalled memory ---\n' + context)

const { answer } = await mnemotrace.search('Where do I work?', scope)
console.log('\nsearch answer:', answer)

// OpenAI-compatible chat with automatic memory injection (requires MNEMOTRACE_LLM on the server).
if (status.llm_configured) {
  const completion = await mnemotrace.chat.completions.create({
    model: 'mnemotrace',
    messages: [{ role: 'user', content: 'Remind me where I work.' }],
    memory: { space_id: spaceId, session_id: sessionId, scope: 'auto' },
  })
  console.log('\nchat answer:', completion.choices[0].message.content)
  console.log('memory used:', completion.mnemotrace)
} else {
  console.log('\n(chat skipped — set MNEMOTRACE_LLM on the server to enable generation)')
}

const report = await mnemotrace.sessionReport(sessionId, scope)
console.log('\n--- session memory report ---')
console.log({
  episodes: report.episodes,
  factsAdded: report.facts_added,
  factsRedacted: report.facts_redacted,
  facts: report.facts.map((f) => f.text),
})

const sessions = await mnemotrace.sessions({ ...scope, limit: 10, query: 'sdk-demo' })
console.log('\n--- cross-agent session index ---')
console.log(sessions.sessions.map((s) => ({
  id: s.id,
  episodes: s.episodes,
  factsAdded: s.facts_added,
  workingLive: s.working_live,
})))

const memoryPage = await mnemotrace.memories({
  ...scope,
  factsLimit: 10,
  episodesLimit: 0,
  status: 'live',
})
console.log('\n--- user-owned memory page ---')
console.log({
  totalFacts: memoryPage.facts_page.total,
  nextFactOffset: memoryPage.next_offsets.facts,
  facts: memoryPage.facts.map((f) => ({
    id: f.id,
    text: f.display ?? f.text,
    sensitive: f.sensitive ?? false,
  })),
})

const portable = await mnemotrace.export(scope)
console.log('\n--- share-safe export ---')
console.log({
  includeSensitive: portable.include_sensitive,
  redactedSensitive: portable.redacted_sensitive,
  facts: portable.facts.length,
  episodes: portable.episodes.length,
})
