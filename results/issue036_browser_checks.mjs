import assert from 'node:assert/strict';

// Run after loading the production Facts page with installFixture and logging in
// using a fresh synthetic key (default hideSensitive=false).
export async function checkFacts(page) {
  const records = [];
  const snapshot = () => page.evaluate(() => ({
    text: document.querySelector('main').innerText,
    hidden: document.querySelector('[aria-label="隐藏敏感内容"]')?.getAttribute('aria-checked'),
    searchPresent: !!document.querySelector('[aria-label="搜索事实…"]'),
    pending: window.__issue036.pending.map(p => p.params),
  }));
  const record = async name => { const state = await snapshot(); records.push({ name, state }); return state; };
  const absent = state => assert.ok(!state.text.includes('synthetic sensitive memory'));
  const release = failure => page.evaluate(fail => window.__issue036.pending.shift().finish(fail), failure);
  const pending = () => page.waitForFunction(() => window.__issue036.pending.length > 0);
  const toggle = () => page.click('loc=css:button[aria-label="隐藏敏感内容"]');

  assert.match((await record('initial')).text, /synthetic sensitive memory/);
  await toggle();
  await pending();
  let state = await record('hidden_waiting');
  absent(state); assert.equal(state.hidden, 'true'); assert.equal(state.searchPresent, true);
  assert.match(state.text, /加载事实/); assert.match(state.text, /正在统计/);
  assert.ok(!state.text.includes('共 2 条事实'));
  await release(true);
  await page.waitForFunction(() => document.querySelector('main').innerText.includes('重新加载'));
  state = await record('failed'); absent(state); assert.equal(state.searchPresent, true);
  assert.match(state.text, /synthetic request failed/);
  await page.click('loc=role:button[name="重新加载"]');
  await pending();
  absent(await record('retry_waiting'));
  await release(false);
  await page.waitForFunction(() => document.querySelector('main').innerText.includes('共 1 条事实'));
  state = await record('retry_success'); absent(state); assert.match(state.text, /synthetic public memory/);

  await page.evaluate(() => { window.__issue036.hold = true; });
  await page.fill('loc=css:input[placeholder="搜索事实…"]', 'synthetic');
  await pending();
  await toggle();
  await page.waitForFunction(() => window.__issue036.pending.some(p => p.params.include_sensitive === 'true'));
  await toggle();
  state = await record('rapid_toggle_waiting'); absent(state); assert.equal(state.hidden, 'true');
  await page.evaluate(() => {
    const i = window.__issue036.pending.findIndex(p => p.params.include_sensitive === 'true');
    window.__issue036.pending.splice(i, 1)[0].finish(false);
  });
  // Let the rendered query settle after the old wide response; safe response remains held.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  absent(await record('late_wide_response'));
  await page.evaluate(() => { for (const p of window.__issue036.pending.splice(0)) p.finish(false); });
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"]'));
  absent(await record('rapid_toggle_success'));

  await page.fill('loc=css:input[placeholder="搜索事实…"]', 'no-synthetic-match');
  await pending(); await release(false);
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"]'));
  state = await record('empty_success'); assert.ok(!state.text.includes('synthetic public memory'));
  await page.fill('loc=css:input[placeholder="搜索事实…"]', 'public');
  await pending();
  state = await record('empty_placeholder'); assert.match(state.text, /正在更新事实/);
  assert.ok(!state.text.includes('还没有事实')); absent(state);
  await release(false);
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"]'));
  absent(await record('search_success'));
  return records;
}
