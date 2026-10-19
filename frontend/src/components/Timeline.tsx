import type { MemoryFact } from '../types'
import { copy as t } from '../lib/copy'
import { splitStamp } from '../lib/format'
import { cx } from './ui'

// 事实按最新优先到达（服务端按 valid_at 倒序排列）。竖向轨道 + 彩色圆点让双时间轴演化
// 一目了然：绿色 = 当前为真，灰色/删除线 = 已被取代（被后来的事实替换），
// “更新”标签标记替换了旧值的事实。
export function Timeline({ facts }: { facts: MemoryFact[] }) {
  return (
    <div className="relative">
      <ul className="space-y-1">
        {facts.map((f, index) => {
          const live = f.status === 'live'
          const { date, time } = splitStamp(f.valid_at)
          return (
            <li key={f.id} className="relative flex items-start gap-4 py-2">
              {/* 线段跨过行间距，在当前圆点下方和下一圆点上方各留 5px。 */}
              {index < facts.length - 1 && (
                <span
                  aria-hidden="true"
                  className="timeline-rail absolute -bottom-3 left-[109px] top-[28px] w-px -translate-x-1/2"
                />
              )}
              <time className="w-[88px] shrink-0 pt-0.5 text-right tabular-nums leading-tight text-brand-cyan">
                <span className="block text-[11.5px]">{date}</span>
                {time && <span className="block text-[10px] text-brand-cyan/55">{time}</span>}
              </time>
              <span
                role="img"
                aria-label={live ? t.timeline.legendLive : t.timeline.legendOld}
                title={live ? t.timeline.legendLive : t.timeline.legendOld}
                className={cx(
                  'relative z-10 mt-[5px] h-2.5 w-2.5 shrink-0 rounded-full',
                  live ? 'timeline-live-dot' : 'timeline-old-dot',
                )}
              />
              <div className="min-w-0 flex-1">
                <span
                  className={cx('break-all text-sm', !live && 'text-ghost line-through')}
                  title={!live ? `${t.common.superseded}${f.invalid_at ? ` · ${f.invalid_at}` : ''}` : undefined}
                >
                  {f.display || f.text}
                </span>
                <span className="ml-2 inline-flex flex-wrap items-center gap-1.5 align-middle">
                  {f.source === 'user' && (
                    <span
                      className="fact-blue-tag shrink-0 text-xs"
                      title={t.profile.iconHints.user}
                    >
                      {t.profile.evidenceUser}
                    </span>
                  )}
                  {f.supersedes && (
                    <span
                      className="fact-blue-tag shrink-0 text-xs"
                      title={t.timeline.legendUpdateNote}
                    >
                      {t.common.updated}
                    </span>
                  )}
                </span>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
