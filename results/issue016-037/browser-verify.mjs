import assert from 'node:assert/strict'
import { appendFile } from 'node:fs/promises'
import { installNetwork } from './browser-network.mjs'
import { directory, draft, login, markDraft, assertDraft, pending, record, release, rules } from './browser-common.mjs'

// Import with ?dev=1 to also verify a separately started Vite proxy on :5174.
// Use only serve-validation.py's isolated synthetic accounts and data directory.
const withDev = new URL(import.meta.url).searchParams.get('dev') === '1'
const task = await taskSpace('MnemoTrace console acceptance'), page = task.page('p1')
const settings = 'textarea.settings-textarea'
const tick = () => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)))
const noAlert = () => page.waitForFunction(() => !document.querySelector('[role="alert"]'))
const ready = selector => page.waitForFunction(selector => document.querySelector(selector), selector)
async function staleWindow() {
  // main.tsx keeps successful queries fresh for 10s; use the real clock before reconnecting.
  const deadline = await page.evaluate(() => Date.now() + 10_100)
  await page.waitForFunction(deadline => Date.now() >= deadline, deadline, { timeout: 15_000 })
}
async function reconnect() {
  await staleWindow()
  await page.evaluate(() => window.dispatchEvent(new Event('offline')))
  await tick()
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
}
async function nav(path) {
  const menuVisible = await page.evaluate(() => {
    const menu = document.querySelector('button[aria-label="打开导航"]')
    return !!menu?.getClientRects().length && getComputedStyle(menu).visibility !== 'hidden'
  })
  if (menuVisible) await page.press('loc=role:button[name="打开导航"]', 'Enter')
  await page.press(`loc=href:/ui/${path}`, 'Enter')
  await page.waitForURL(`**/ui/${path}`)
}
async function cycle(path, selector, name, outcome = '503') {
  await pending(page, path)
  await release(page, path, outcome)
  await page.waitForFunction(() => document.querySelector('[role="alert"]'))
  await assertDraft(page, selector)
  await record(page, `consolidated_${name}_failure`)
  await page.press('loc=role:button[name="重试"]', 'Enter')
  await pending(page, path)
  await assertDraft(page, selector)
  await record(page, `consolidated_${name}_retry_waiting`)
  await rules(page, [])
  await release(page, path)
  await noAlert()
  await assertDraft(page, selector)
  await record(page, `consolidated_${name}_recovered`)
}
async function addFact(fact) {
  const response = await page.fetch('/v1/facts', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic-console-a' },
    body: JSON.stringify(fact),
  })
  assert.equal(response.status, 200)
}
console.log({ spaceId: task.spaceId, mode: 'verification', withDev })
const init = await page.cdp('Page.addScriptToEvaluateOnNewDocument', {
  source: `(${installNetwork.toString()})(); window.__consoleValidation.rules = [{path:'/v1/sessions',params:{q:'codex'}}];`,
})
try {
  await page.goto('http://127.0.0.1:8000/ui/settings')
  console.log(await page.snapshot())
  for (const path of ['/health', '/ready']) assert.equal((await page.fetch(path)).status, 200)
  for (const key of ['synthetic-console-a', 'synthetic-console-b']) {
    const seeded = await page.fetch('/v1/remember', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
      body: JSON.stringify({ content: key.endsWith('a') ? 'I live in Testville.' : 'I live in OtherTestville.', session_id: 'synthetic-session', scope: 'long' }),
    })
    assert.equal(seeded.status, 200)
  }
  await login(page)
  await ready(settings)
  await markDraft(page, settings)
  await cycle('/v1/sessions', settings, 'settings')
  await rules(page, [{ path: '/v1/policy' }])
  await reconnect()
  await cycle('/v1/policy', settings, 'policy_refresh')

  const documentId = await page.evaluate(() => window.__consoleValidation.documentId)
  await page.press('text="最近 Codex 会话"', 'Enter')
  await rules(page, [{ path: '/v1/working' }])
  await page.press('loc=role:link[name="查看会话记录"]', 'Enter')
  await page.waitForURL('**/ui/conversations')
  assert.equal(await page.evaluate(() => window.__consoleValidation.documentId), documentId)
  await ready('textarea')
  await markDraft(page, 'textarea')
  await cycle('/v1/working', 'textarea', 'conversation', 'network')
  await record(page, 'consolidated_router_navigation', { sameDocument: true })
  await page.evaluate(() => history.back())
  await page.waitForURL('**/ui/settings')
  await page.evaluate(() => history.forward())
  await page.waitForURL('**/ui/conversations')
  assert.equal(await page.evaluate(() => window.__consoleValidation.documentId), documentId)
  await record(page, 'consolidated_history', { sameDocument: true })

  await rules(page, [{ path: '/v1/conflicts' }])
  await nav('facts')
  await ready('button[aria-label="编辑"]')
  console.log(await page.snapshot())
  await page.press('loc=css:button[aria-label="编辑"] >> nth=0', 'Enter')
  const factInput = '.fixed.inset-0.z-50 label:last-child input'
  await markDraft(page, factInput)
  await page.evaluate(() => { window.__factInputs = [...document.querySelectorAll('.fixed.inset-0.z-50 label input')].map(node => ({ node, value: node.value })) })
  await cycle('/v1/conflicts', factInput, 'fact')
  assert.equal(await page.evaluate(() => window.__factInputs.every(({ node, value }) => node.isConnected && node.value === value)), true)
  await page.press(`loc=css:${factInput}`, 'Escape')
  await rules(page, [{ path: '/v1/memories', params: { episodes_limit: '200', facts_limit: '0' } }])
  await page.press('loc=css:button[aria-label="查看来源"] >> nth=0', 'Enter')
  await pending(page, '/v1/memories')
  assert.equal(await page.evaluate(() => document.body.innerText.includes('正在加载原始对话')), true)
  await release(page, '/v1/memories', '503')
  await page.waitForFunction(() => document.body.innerText.includes('来源读取失败'))
  assert.equal(await page.evaluate(() => document.body.innerText.includes('来源对话已不在库中')), false)
  await record(page, 'consolidated_source_failure')
  await rules(page, [])
  await page.press('loc=role:button[name="重新加载"]', 'Enter')
  await page.waitForFunction(() => document.body.innerText.includes('I live in Testville.'))
  await record(page, 'consolidated_source_recovered')
  await page.press('loc=css:button[aria-label="取消"]', 'Enter')

  await rules(page, [{ path: '/v1/sessions', params: { q: 'codex' } }])
  await staleWindow()
  await nav('settings')
  await ready(settings)
  await markDraft(page, settings)
  await pending(page, '/v1/sessions')
  await page.press('loc=role:button[name="退出"]', 'Enter')
  await login(page, 'synthetic-console-b')
  await ready(settings)
  assert.notEqual(await page.evaluate(selector => document.querySelector(selector).value, settings), draft)
  await release(page, '/v1/sessions', '503')
  await tick()
  assert.equal(await page.evaluate(() => !!document.querySelector('[role="alert"]')), false)
  await record(page, 'consolidated_account_switch_old_failure_ignored')
  await pending(page, '/v1/sessions')
  await release(page, '/v1/sessions', '503')
  await page.waitForFunction(() => document.querySelector('[role="alert"]'))
  await rules(page, [])
  await page.press('loc=role:button[name="重试"]', 'Enter')
  await noAlert()
  await record(page, 'consolidated_current_account_retry')
  await page.press('loc=role:button[name="退出"]', 'Enter')
  await rules(page, [{ path: '/v1/sessions', params: { q: 'codex' } }])
  await login(page)
  await ready(settings)
  await markDraft(page, settings)
  await pending(page, '/v1/sessions')
  await page.press('loc=role:button[name="退出"]', 'Enter')
  await login(page)
  await ready(settings)
  assert.notEqual(await page.evaluate(selector => document.querySelector(selector).value, settings), draft)
  await rules(page, [])
  await page.evaluate(() => { for (const request of window.__consoleValidation.pending.splice(0)) request.finish('real') })
  await tick()
  await noAlert()
  await record(page, 'consolidated_same_account_new_epoch')

  const runId = Date.now().toString(36), subject = `synthetic-regroup-${runId}`
  const original = `Synthetic original ${runId}`
  await addFact({ subject, predicate: 'decision', object: original })
  await nav('journal')
  await page.waitForFunction(text => document.querySelector('main').innerText.includes(text), original)
  console.log(await page.snapshot())
  const editIndex = await page.evaluate(text => [...document.querySelectorAll('button[aria-label="编辑"]')].findIndex(button => button.closest('li')?.innerText.includes(text)), original)
  assert.ok(editIndex >= 0)
  await page.press(`loc=css:button[aria-label="编辑"] >> nth=${editIndex}`, 'Enter')
  await markDraft(page, 'textarea')
  for (const [name, fact] of [
    ['new_group', { subject: `${subject}-other`, predicate: 'lesson', object: `Synthetic new group ${runId}` }],
    ['split_group', { subject, predicate: 'finding', object: `Synthetic same session update ${runId}` }],
  ]) {
    await rules(page, [{ path: '/v1/memories', params: { kind: 'outcomes' } }])
    await reconnect()
    await pending(page, '/v1/memories')
    await release(page, '/v1/memories', '503')
    await page.waitForFunction(() => document.querySelector('main').innerText.includes('结论更新失败'))
    await assertDraft(page, 'textarea')
    await record(page, `consolidated_journal_${name}_failure`)
    await addFact(fact)
    await rules(page, [])
    await page.press('loc=role:button[name="重新加载"]', 'Enter')
    await page.waitForFunction(text => document.querySelector('main').innerText.includes(text) && !document.querySelector('[role="alert"]'), fact.object)
    await assertDraft(page, 'textarea')
    await record(page, `consolidated_journal_${name}_recovered`, { sameInputNode: true })
  }
  await page.cdp('Page.removeScriptToEvaluateOnNewDocument', { identifier: init.identifier })
  await page.reload()
  await ready('#login-api-key')
  await record(page, 'consolidated_explicit_reload_requires_login')
  if (withDev) {
    await page.goto('http://127.0.0.1:5174/ui/settings')
    await page.evaluate(installNetwork)
    await login(page)
    await ready(settings)
    await page.press('text="最近 Codex 会话"', 'Enter')
    const id = await page.evaluate(() => window.__consoleValidation.documentId)
    await page.press('loc=role:link[name="查看会话记录"]', 'Enter')
    await page.waitForURL('**/ui/conversations')
    await ready('textarea')
    assert.equal(await page.evaluate(() => window.__consoleValidation.documentId), id)
    await record(page, 'consolidated_development_router', { sameDocument: true })
  }
  console.log(await page.snapshot())
  await task.finish({ keep: [] })
  await appendFile(`${directory}/validation.jsonl`, JSON.stringify({ label: 'consolidated_browser_cleanup', date: new Date().toISOString(), spaceId: task.spaceId, closed: true, withDev }) + '\n')
} catch (error) {
  await record(page, 'consolidated_browser_failure', { status: 'failed', error: String(error) })
  console.log(await page.snapshot())
  throw error
}
