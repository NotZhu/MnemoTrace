// Run against a local MnemoTrace server:
//   pip install "mnemotrace-memory[serve]"
//   MNEMOTRACE_OPEN=1 MNEMOTRACE_EMBEDDER=hashing MNEMOTRACE_LLM=deepseek uvicorn mnemotrace.server.app:app
//   node examples/node.mjs
//
// (After `npm run build`, import from 'mnemotrace-memory'. Here we import the built dist directly.)
import { MnemoTraceClient } from '../dist/index.js'

const mnemotrace = new MnemoTraceClient({
  baseUrl: process.env.MNEMOTRACE_URL ?? 'http://localhost:8000',
  apiKey: process.env.MNEMOTRACE_API_KEY ?? 'demo-user',
})

const sessionId = process.env.MNEMOTRACE_SESSION_ID ?? 'sdk-demo:node'

const status = await mnemotrace.agentStatus({ sessionId })
console.log('--- agent memory status ---')
console.log({
  namespace: status.user,
  session: status.session,
  focus: status.focus,
  facts: status.counts.facts_live,
})

await mnemotrace.remember('I live in Shenzhen and my favorite language is Python.', { sessionId })
await mnemotrace.remember('Actually I just switched jobs — I now work at Moonshot AI.', { sessionId })

const { context } = await mnemotrace.recall('where do I work and live?', { sessionId })
console.log('--- recalled memory ---\n' + context)

const { answer } = await mnemotrace.search('What is my favorite programming language?')
console.log('\nsearch answer:', answer)

// OpenAI-compatible chat with automatic memory injection (requires MNEMOTRACE_LLM on the server).
try {
  const completion = await mnemotrace.chat.completions.create({
    model: 'mnemotrace',
    messages: [{ role: 'user', content: 'Remind me where I work.' }],
    memory: { session_id: sessionId, scope: 'auto' },
  })
  console.log('\nchat answer:', completion.choices[0].message.content)
  console.log('memory used:', completion.mnemotrace)
} catch (err) {
  console.log('\n(chat skipped — set MNEMOTRACE_LLM on the server to enable generation)')
}

await mnemotrace.closeSession(sessionId)
console.log('\nclosed memory session:', sessionId)

const report = await mnemotrace.sessionReport(sessionId)
console.log('\n--- session memory report ---')
console.log({
  episodes: report.episodes,
  factsAdded: report.facts_added,
  factsRedacted: report.facts_redacted,
  facts: report.facts.map((f) => f.text),
})

const sessions = await mnemotrace.sessions({ limit: 10, query: 'sdk-demo' })
console.log('\n--- cross-agent session index ---')
console.log(sessions.sessions.map((s) => ({
  id: s.id,
  episodes: s.episodes,
  factsAdded: s.facts_added,
  workingLive: s.working_live,
})))

const memoryPage = await mnemotrace.memories({
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

const portable = await mnemotrace.export()
console.log('\n--- share-safe export ---')
console.log({
  includeSensitive: portable.include_sensitive,
  redactedSensitive: portable.redacted_sensitive,
  facts: portable.facts.length,
  episodes: portable.episodes.length,
})
