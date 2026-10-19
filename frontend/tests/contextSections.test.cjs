const assert = require('node:assert/strict')
const test = require('node:test')
const path = require('node:path')
const vm = require('node:vm')
const esbuild = require('esbuild')

const source = esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, '../src/lib/contextSections.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false,
}).outputFiles[0].text
const moduleData = { exports: {} }
vm.runInNewContext(source, { module: moduleData, exports: moduleData.exports })
const { splitContextSections } = moduleData.exports

test('中文画像四个小节保留在同一卡片，偏好表独立分区', () => {
  const profile = '身份与背景：\n居住在示例城市。\n\n兴趣与偏好：\n喜欢茶。\n\n限制与目标：\n计划阅读。\n\n回答与协助偏好：\n先给结论。'
  const preferences = '日期 | 人物或对象 | 偏好关系 | 具体内容\n--- | --- | --- | ---\n2026-10-16 | user | likes | 茶'
  const sections = splitContextSections(`USER PROFILE:\n${profile}\n\n当前偏好记录（结构化）：\n${preferences}\n\nFACTS (current, dated):\nuser likes 茶`)
  assert.equal(sections.length, 3)
  assert.equal(sections[0].key, 'sectionUserProfile')
  assert.equal(sections[0].body, profile)
  assert.equal(sections[1].rawLabel, '当前偏好记录（结构化）')
  assert.equal(sections[1].body, preferences)
  assert.equal(sections[2].key, 'sectionFacts')
})

test('旧英文 STYLE 合并到画像，旧标题和正文保留原文', () => {
  const profile = 'IDENTITY & ANCHORS:\nLives in Example City.\n\nSTYLE:\nPrefers concise answers.'
  const sections = splitContextSections(`USER PROFILE:\n${profile}\n\nPREFERENCE RECORDS (current, structured):\ndate | subject | preference | value\n\nSESSION SUMMARIES (relevant, chronological):\nA summary.`)
  assert.equal(sections.length, 3)
  assert.equal(sections[0].body, profile)
  assert.equal(sections[1].rawLabel, 'PREFERENCE RECORDS (current, structured)')
  assert.equal(sections[2].key, 'sectionSummaries')
})

test('中文顶层标题和 Windows 换行均能识别，保留事实关系值', () => {
  const sections = splitContextSections('用户画像：\r\n回答与协助偏好：\r\n简短回答。\r\n\r\n相关事实（含日期）：\r\nuser dislikes 番茄\r\n\r\n原文片段：\r\n我不喜欢番茄。')
  assert.deepEqual(Array.from(sections, section => section.key), ['sectionUserProfile', 'sectionFacts', 'sectionConversations'])
  assert.equal(sections[1].body, 'user dislikes 番茄')
})

test('其他英文证据类型仍独立分区，正文空行不丢失', () => {
  const context = 'FACTS (current, dated):\nA fact.\n\nGRAPH PATHS (source-backed):\nA → B\n\nPROVENANCE RAW:\nLine one.\n\nLine two.'
  const sections = splitContextSections(context)
  assert.equal(sections.length, 3)
  assert.equal(sections[1].rawLabel, 'GRAPH PATHS (source-backed)')
  assert.equal(sections[2].body, 'Line one.\n\nLine two.')
  assert.equal(splitContextSections(' \n\n ').length, 0)
})
