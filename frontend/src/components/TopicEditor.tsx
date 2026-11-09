import { useId } from 'react'

import { TOPIC_LABELS, TOPIC_OPTIONS, TOPIC_SOURCE_LABELS, type TopicAction } from '../lib/topics'
import type { TopicMetadata } from '../types'

export function TopicBadge({ topic, topic_source }: TopicMetadata) {
  const label = TOPIC_LABELS[topic]
  const origin = TOPIC_SOURCE_LABELS[topic_source]
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs text-ghost"
      title={`主题：${label}；主题依据：${origin}`} aria-label={`主题：${label}；主题依据：${origin}`}>
      <span className="rounded-md bg-selected px-1.5 py-0.5 text-brand-cyan">{label}</span>
      <span>{origin}</span>
    </span>
  )
}

export function TopicEditor({ current, value, onChange, disabled = false }: {
  current?: TopicMetadata
  value: TopicAction
  onChange: (value: TopicAction) => void
  disabled?: boolean
}) {
  const id = useId()
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={id} className="text-sm text-ghost">主题</label>
        {current && <TopicBadge {...current} />}
      </div>
      <select id={id} aria-describedby={`${id}-hint`} className="input w-full"
        value={value} onChange={event => onChange(event.target.value as TopicAction)} disabled={disabled}>
        <option value="keep">{current ? '不修改主题' : '自动判断'}</option>
        {current && <option value="reset">清除主题</option>}
        {TOPIC_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <p id={`${id}-hint`} className="text-xs leading-relaxed text-ghost">
        {value === 'reset' ? '保存后主题会清为未知。'
          : value !== 'keep' ? '保存为人工主题；选择“未知”也会保留为人工设置。'
          : current ? '仅修改其他元数据会保留当前主题；正文变化后主题清为未知。'
          : '未指定时主题为未知。'}
      </p>
    </div>
  )
}
