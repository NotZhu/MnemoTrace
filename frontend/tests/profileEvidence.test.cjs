const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const fs = require('node:fs')
const vm = require('node:vm')
const esbuild = require('esbuild')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/pages/Profile.tsx')],
  bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'lucide-react', '../components/Toast', '../hooks/queries'],
}).outputFiles[0].text
const moduleData = { exports: {} }
vm.runInNewContext(source, {
  module: moduleData, exports: moduleData.exports,
  require: name => {
    if (name === '../components/Toast' || name === '../hooks/queries') return {}
    return require(name)
  },
})
const { ProfileTag } = moduleData.exports
const render = (evidence, props = {}) => renderToStaticMarkup(React.createElement(ProfileTag, { text: '咖啡', e: evidence, ...props }))
const visibleText = html => html.replace(/<[^>]+>/g, '')

test('普通标签仅提示强化与来源数，不出现自动提取文字或手动折角', () => {
  const html = render({ kind: 'mentions', source_count: 2, reinforcement_count: 0 })
  assert.equal(visibleText(html), '咖啡x2')
  assert.match(html, /data-manual="false"/)
  assert.match(html, /title="强化 0 次 · 来源 2 条"/)
  assert.doesNotMatch(html, /自动提取/)
})

test('无来源的手动标签保留折角和内容，不补造来源数', () => {
  const html = render({ kind: 'user', source_count: 0, reinforcement_count: 0 })
  assert.equal(visibleText(html), '咖啡')
  assert.match(html, /data-manual="true"/)
  assert.match(html, /title="手动设定 · 强化 0 次 · 来源 0 条"/)
})

test('手动标签可同时显示强化底色，来源数与强化次数保持独立', () => {
  const html = render({ kind: 'user', source_count: 2, reinforcement_count: 7 })
  assert.equal(visibleText(html), '咖啡x2')
  assert.match(html, /data-manual="true"/)
  assert.match(html, /data-reinforcement-level="3"/)
  assert.match(html, /title="手动设定 · 强化 7 次 · 来源 2 条"/)
})

test('多来源事实按强化次数着色，不依赖 reinforced 状态', () => {
  const html = render({ kind: 'mentions', source_count: 3, reinforcement_count: 20 })
  assert.equal(visibleText(html), '咖啡x3')
  assert.match(html, /data-reinforcement-level="4"/)
  assert.match(html, /强化 20 次/)
  assert.doesNotMatch(visibleText(html), /已增强|x20/)
})

test('无来源的强化事实不生成来源标记', () => {
  const html = render({ kind: 'reinforced', source_count: 0, reinforcement_count: 7 })
  assert.equal(visibleText(html), '咖啡')
  assert.match(html, /data-reinforcement-level="3"/)
})

test('身份补全无来源和强化时保留基础底色', () => {
  const html = render({ kind: 'mentions', source_count: 0, reinforcement_count: 0 })
  assert.equal(visibleText(html), '咖啡')
  assert.match(html, /data-reinforcement-level="0"/)
})

test('强化分档覆盖全部边界并在最高档封顶', () => {
  for (const [count, level] of [[0, 0], [1, 1], [2, 1], [3, 2], [5, 2], [6, 3], [10, 3], [11, 4], [1000, 4]]) {
    const html = render({ kind: 'user', source_count: 1, reinforcement_count: count })
    assert.match(html, new RegExp(`data-reinforcement-level="${level}"`))
    assert.equal(visibleText(html), '咖啡x1')
  }
})

test('三种语义配色保留相同的手动与强化信息', () => {
  for (const tone of ['positive', 'negative', 'neutral']) {
    const html = render({ kind: 'user', source_count: 1, reinforcement_count: 3 }, { tone })
    assert.match(html, new RegExp(`data-tone="${tone}"`))
    assert.match(html, /data-manual="true"/)
    assert.match(html, /data-reinforcement-level="2"/)
  }
})

test('标签支持键盘聚焦，辅助名称包含来源与强化信息，内容保持转义', () => {
  const html = render({ kind: 'user', source_count: 1, reinforcement_count: 4 }, { text: '<img src=x onerror=alert(1)>' })
  assert.match(html, /tabindex="0"/)
  assert.match(html, /aria-label="[^"]*手动设定 · 强化 4 次 · 来源 1 条"/)
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.doesNotMatch(html, /<img/)
})

function luminance(rgb) {
  const channels = rgb.map(value => {
    const channel = value / 255
    return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
  })
  return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
}

test('深浅主题的每一档底色均保持文字可读', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '../src/index.css'), 'utf8')
  const blocks = [css.match(/:root \{([\s\S]*?)\n  \}/)[1], css.match(/:root\[data-theme='light'\] \{([\s\S]*?)\n  \}/)[1]]
  const strengths = [...css.matchAll(/\.profile-tag\[data-reinforcement-level='\d'\] \{ --profile-tag-strength: (\d+)%; \}/g)].map(match => Number(match[1]) / 100)
  assert.equal(strengths.length, 5)
  assert(strengths.every((value, index) => index === 0 || value > strengths[index - 1]))
  for (const [theme, block] of blocks.entries()) {
    const colors = Object.fromEntries([...block.matchAll(/--memory-([\w-]+): (\d+) (\d+) (\d+);/g)].map(match => [match[1], match.slice(2).map(Number)]))
    for (const [tint, foreground] of [['mint', 'profile-mint-text'], ['rose', 'profile-rose-text'], ['accent', 'text']]) {
      for (const opacity of strengths) {
        const surface = colors[tint].map((value, index) => value * opacity + colors.surface[index] * (1 - opacity))
        const levels = [luminance(colors[foreground]), luminance(surface)].sort((a, b) => b - a)
        const ratio = (levels[0] + .05) / (levels[1] + .05)
        assert(ratio >= 4.5, `${theme}:${tint}/${opacity} contrast ${ratio}`)
      }
    }
  }
})
