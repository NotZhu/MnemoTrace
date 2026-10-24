import assert from 'node:assert/strict'
import { appendFile } from 'node:fs/promises'
export const directory = 'E:/MnemoTrace/results/issue016-037'
export const draft = '合成草稿：辅助读取失败时必须保留。'
export async function record(page, name, extra = {}) {
  const state = await page.evaluate(() => ({ url: location.href, documentId: window.__consoleValidation?.documentId,
    inputs: [...document.querySelectorAll('textarea, input:not(#login-api-key)')].map(n => ({ value: n.value, label: n.getAttribute('aria-label') || n.placeholder || n.id })),
    draftNode: window.__draftElement ? { connected: window.__draftElement.isConnected, value: window.__draftElement.value } : null,
    alert: document.querySelector('[role="alert"]')?.innerText ?? null,
    requests: window.__consoleValidation?.requests,
  }))
  await appendFile(`${directory}/validation.jsonl`, JSON.stringify({ label: name, date: new Date().toISOString(), ...state, ...extra }) + '\n')
  console.log({ name, ...extra, inputs: state.inputs, alert: state.alert })
}
export const pending = (page, path) => page.waitForFunction(path => window.__consoleValidation.pending.some(p => p.path === path), path)
export async function release(page, path, outcome = 'real') {
  await page.evaluate(({ path, outcome }) => {
    const index = window.__consoleValidation.pending.findIndex(p => p.path === path)
    if (index < 0) throw new Error(`No pending request for ${path}`)
    window.__consoleValidation.pending.splice(index, 1)[0].finish(outcome)
  }, { path, outcome })
}
export const rules = (page, rules) => page.evaluate(rules => { window.__consoleValidation.rules = rules }, rules)
export async function assertDraft(page, selector, value = draft) {
  const state = await page.evaluate(selector => {
    const input = document.querySelector(selector)
    return { value: input?.value, sameNode: input === window.__draftElement, connected: input?.isConnected }
  }, selector)
  assert.equal(state.value, value)
  assert.equal(state.sameNode, true)
  assert.equal(state.connected, true)
}
export async function markDraft(page, selector) {
  await page.press(`loc=css:${selector}`, 'ControlOrMeta+A')
  await page.keyboard.insertText(draft)
  await page.evaluate(selector => { window.__draftElement = document.querySelector(selector) }, selector)
}
export async function login(page, key = 'synthetic-console-a') {
  await page.press('loc=css:#login-api-key', 'ControlOrMeta+A')
  await page.keyboard.insertText(key)
  await page.press('loc=css:#login-api-key', 'Enter')
  await page.waitForSelector('loc=role:button[name="退出"]')
}
