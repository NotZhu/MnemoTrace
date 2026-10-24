import assert from 'node:assert/strict'
import { installNetwork } from './browser-network.mjs'
import { draft, login, markDraft, pending, record, release, rules } from './browser-common.mjs'

// Run against the old production UI hosted by the isolated validation service.
const task = await taskSpace('MnemoTrace console baseline'), page = task.page('p1')
console.log({ spaceId: task.spaceId, mode: 'baseline' })
await page.cdp('Page.addScriptToEvaluateOnNewDocument', {
  source: `(${installNetwork.toString()})(); window.__consoleValidation.rules = [{path:'/v1/sessions',params:{q:'codex'}}];`,
})
await page.goto('http://127.0.0.1:8000/ui/settings')
console.log(await page.snapshot())
await login(page)
await page.waitForFunction(() => document.querySelector('textarea.settings-textarea'))
await markDraft(page, 'textarea.settings-textarea')
await pending(page, '/v1/sessions')
await release(page, '/v1/sessions', '503')
await page.waitForFunction(() => document.querySelector('[role="alert"]'))
assert.equal(await page.evaluate(() => !!document.querySelector('textarea.settings-textarea')), false, 'old UI must unmount the editor')
await record(page, 'consolidated_baseline_failure', { result: 'reproduced' })
await rules(page, [])
await page.press('loc=role:button[name="重试"]', 'Enter')
await page.waitForFunction(() => document.querySelector('textarea.settings-textarea'))
assert.notEqual(await page.evaluate(() => document.querySelector('textarea.settings-textarea').value), draft)
await record(page, 'consolidated_baseline_recovered', { result: 'draft lost as expected on old UI' })
console.log(await page.snapshot())
await task.finish({ keep: [] })
