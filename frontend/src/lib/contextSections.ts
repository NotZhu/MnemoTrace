import type { Copy } from './copy'

type SectionKey = Extract<
  keyof Copy['ask'],
  'sectionUserProfile' | 'sectionFacts' | 'sectionTimeline' | 'sectionDigests' | 'sectionConversations'
>

export interface ContextSection {
  key?: SectionKey
  rawLabel: string
  body: string
}

const SECTION_KEYS: Array<[RegExp, SectionKey]> = [
  [/^(?:USER PROFILE|用户画像)/i, 'sectionUserProfile'],
  [/^(?:FACTS|相关事实)/i, 'sectionFacts'],
  [/^(?:TIMELINE|时间线)/i, 'sectionTimeline'],
  [/^(?:SESSION DIGESTS|会话摘要)/i, 'sectionDigests'],
  [/^(?:RELEVANT CONVERSATIONS|原文片段)/i, 'sectionConversations'],
]

const BLOCK_TITLE = /^(?:[A-Z][A-Z ]+(?:\([^)]*\))?|当前偏好记录（结构化）|用户画像|相关事实(?:（含日期）)?|时间线|会话摘要|原文片段)[:：]$/
const PROFILE_TITLE = /^(?:IDENTITY & ANCHORS|INTERESTS & PREFERENCES|CONSTRAINTS & GOALS|STYLE|身份与背景|兴趣与偏好|限制与目标|回答与协助偏好)[:：]$/i

export function splitContextSections(context: string): ContextSection[] {
  const sections: ContextSection[] = []
  for (const paragraph of context.trim().split(/\r?\n[ \t]*\r?\n/)) {
    if (!paragraph.trim()) continue
    const [head, ...lines] = paragraph.split(/\r?\n/)
    const title = head.trim()
    const previous = sections[sections.length - 1]
    // 新中文画像与旧英文 STYLE 都属于画像正文，不能因内部标题多拆一张卡。
    const insideProfile = previous?.key === 'sectionUserProfile' && PROFILE_TITLE.test(title)
    if (!previous || (BLOCK_TITLE.test(title) && !insideProfile)) {
      const key = SECTION_KEYS.find(([pattern]) => pattern.test(title))?.[1]
      sections.push({ key, rawLabel: title.replace(/[:：]$/, ''), body: lines.join('\n').trim() || title })
    } else {
      previous.body += `\n\n${paragraph}`
    }
  }
  return sections
}
