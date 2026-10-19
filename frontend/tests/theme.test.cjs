const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const fs = require('node:fs')
const vm = require('node:vm')
const esbuild = require('esbuild')

const bundled = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/store/theme.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
  external: ['react'],
}).outputFiles[0].text

function load(saved, unavailable = false, reducedMotion = false) {
  const values = new Map(saved == null ? [] : [['mnemotrace.theme', JSON.stringify({state:{theme:saved}, version:0})]])
  const localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const document = { documentElement: { dataset: {}, style: {} } }
  const timers = new Map()
  let timerId = 0
  const setTimeout = callback => { timers.set(++timerId, callback); return timerId }
  const clearTimeout = id => timers.delete(id)
  const window = { matchMedia: () => ({matches: reducedMotion}) }
  Object.defineProperty(window, 'localStorage', {get: () => {
    if (unavailable) throw new Error('storage unavailable')
    return localStorage
  }})
  const moduleData = {exports:{}}
  vm.runInNewContext(bundled, {module:moduleData, exports:moduleData.exports, require, window, document, setTimeout, clearTimeout, console:{warn(){}}})
  return {store:moduleData.exports.useTheme, document, values, timers,
    finishTransitions() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback()) },
  }
}

test('默认深色，切换即时应用到页面并保存，重新加载恢复浅色', () => {
  const state = load()
  assert.equal(state.document.documentElement.dataset.theme, 'dark')
  state.store.getState().toggle()
  assert.equal(state.document.documentElement.dataset.theme, 'light')
  assert.equal(state.document.documentElement.style.colorScheme, 'light')
  const saved = JSON.parse(state.values.get('mnemotrace.theme'))
  assert.deepEqual(saved.state, {theme:'light'})
  const reloaded = load(saved.state.theme)
  assert.equal(reloaded.store.getState().theme, 'light')
  assert.equal(reloaded.document.documentElement.dataset.theme, 'light')
  reloaded.store.getState().toggle()
  assert.equal(reloaded.document.documentElement.dataset.theme, 'dark')
})

test('无效偏好回退深色，存储不可用时仍能切换', () => {
  assert.equal(load('invalid').store.getState().theme, 'dark')
  const state = load(null, true)
  state.store.getState().toggle()
  assert.equal(state.store.getState().theme, 'light')
  assert.equal(state.document.documentElement.dataset.theme, 'light')
})

test('首次恢复不渐变，手动切换同步颜色过渡，快速连点只保留最后一个清理任务', () => {
  const state = load('light')
  const root = state.document.documentElement
  assert.equal(root.dataset.themeTransition, undefined)
  state.store.getState().toggle()
  assert.equal(root.dataset.themeTransition, 'true')
  state.store.getState().toggle()
  assert.equal(root.dataset.theme, 'light')
  assert.equal(root.dataset.themeTransition, 'true')
  assert.equal(state.timers.size, 1)
  state.finishTransitions()
  assert.equal(root.dataset.themeTransition, undefined)
})

test('减少动态效果时不启用新增的颜色渐变或清理定时器', () => {
  const state = load('dark', false, true)
  state.store.getState().toggle()
  assert.equal(state.document.documentElement.dataset.theme, 'light')
  assert.equal(state.document.documentElement.dataset.themeTransition, undefined)
  assert.equal(state.timers.size, 0)
})

function luminance(rgb) {
  const [r,g,b] = rgb.map(n => {
    const channel = n / 255
    return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
  })
  return .2126*r + .7152*g + .0722*b
}
function contrast(a, b) {
  const [light, dark] = [luminance(a), luminance(b)].sort((a,b)=>b-a)
  return (light + .05) / (dark + .05)
}

test('两套主题的正文、辅助文字及状态文本在对应表面上具有足够对比度', () => {
  const css = fs.readFileSync(path.resolve(__dirname,'../src/index.css'),'utf8')
  const blocks = [css.match(/:root \{([\s\S]*?)\n  \}/)[1], css.match(/:root\[data-theme='light'\] \{([\s\S]*?)\n  \}/)[1]]
  for (const [index, block] of blocks.entries()) {
    const colors = Object.fromEntries([...block.matchAll(/--memory-([\w-]+): (\d+) (\d+) (\d+);/g)].map(m=>[m[1],m.slice(2).map(Number)]))
    for (const foreground of ['text','muted','accent','slate-100','slate-200','slate-300']) {
      for (const surface of ['background','surface','field']) {
        assert(contrast(colors[foreground],colors[surface]) >= 4.5, `${index}:${foreground}/${surface}`)
      }
    }
    assert(contrast(colors.text,colors.button) >= 4.5, `${index}:button label`)
    for (const foreground of ['mint','rose','violet','amber']) {
      const surface = colors[foreground].map((n,i)=>n*.15 + colors.surface[i]*.85)
      assert(contrast(colors[foreground],surface) >= 4.5, `${index}:${foreground} badge`)
    }
  }
})
