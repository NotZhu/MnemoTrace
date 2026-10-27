import type { FactEdit, Topic, TopicSource } from '../types'

export const TOPIC_LABELS: Record<Topic, string> = {
  food: '饮食', travel: '出行', music: '音乐', film_tv: '影视', sports: '运动', reading: '阅读',
  hobbies: '兴趣', environment: '环境', health: '健康', work: '工作', learning: '学习',
  technology: '技术', finance: '财务', relationships: '人际', unknown: '未知',
}
export const TOPIC_OPTIONS = (Object.entries(TOPIC_LABELS) as [Topic, string][])
  .map(([value, label]) => ({ value, label }))
export const TOPIC_SOURCE_LABELS: Record<TopicSource, string> = {
  unset: '未确定', rule: '规则判断', llm: '模型判断', user: '人工设置',
}
export type TopicAction = 'keep' | 'reset' | Topic
export function topicPatch(action: TopicAction): Pick<FactEdit, 'topic' | 'reset_topic'> {
  if (action === 'keep') return {}
  if (action === 'reset') return { reset_topic: true }
  return { topic: action }
}

export function savedTopic(value: unknown): Topic | '' {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(TOPIC_LABELS, value) ? value as Topic : ''
}
