"""Synthetic continuous-session workloads and an open-loop, FIFO client replay.

Only the producer owns the arrival clock. A namespace consumer may fall behind;
that backlog is measured rather than slowing the offered stream to model speed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import queue
import time

SUITE = Path(__file__).parent / 'fixtures' / 'buffered_replay.json'
PILOT = ('burst-short', 'continuous-short', 'inflight-short')


def build_suite():
    """Authoring helper. The checked-in JSON, not this helper, freezes model runs."""
    cases = []
    padding = ('这些陈述属于同一次合成会话，请按各条消息的原意理解。'
               '叙述顺序代表信息更新的顺序，过去的状态和现在的状态需要区分。'
               '没有明确说明的背景不要自行补充。后续如果出现指代，可以结合前文理解，'
               '但不要把解释性说明当作我的个人经历。')
    for family in ('burst', 'continuous', 'idle', 'longinput', 'inflight', 'isolation'):
        for size, count in (('short', 8), ('long', 24)):
            case = dict(id=f'{family}-{size}', category=family, split='development', core=size == 'short',
                        events=[], facts=[], working=[], identity={}, controls=[])
            previous = {}
            for i in range(count):
                block, slot = divmod(i, 8)
                user = 'uv'[i % 2] if family == 'isolation' else 'u'
                session = ('st'[(i // 2) % 2] if family == 'isolation' else 's')
                scope = 'auto' if family != 'isolation' or (i // 4) % 2 == 0 else 'long'
                if family == 'continuous':
                    delay = i * 4
                elif family == 'idle':
                    per = 4 if size == 'short' else 8
                    delay = (i // per) * (per + 8) + i % per
                elif family == 'inflight':
                    delay = i * .1 if i < count // 2 else 3 + (i - count // 2) * .25
                else:
                    delay = i * .25
                event = dict(id=f'e{i+1}', user=user, session=session, scope=scope,
                             at=1_700_000_001 + i, delay=delay)
                key = event['id']
                def fact(predicate, obj, variants=(), *, subject=None, sources=None):
                    row = dict(id=f'f{i+1}', variants=[[subject or user, predicate, obj], *variants],
                               sources=sources or [[key]], at=key)
                    case['facts'].append(row)
                    return row
                if family == 'isolation':
                    obj = f'合成工具{chr(65+i)}'
                    text = f'我拥有一件名叫{obj}的工具。这条所有权信息仅属于当前用户。'
                    fact('owns', obj, [[user, 'has', obj]])
                elif slot == 0:
                    if block == 0:
                        text = '我叫林澈，这是我的姓名。'
                        case['identity'][user] = ['林澈']
                    else:
                        obj = ('游泳', '摄影')[block-1]
                        text = f'我长期喜欢{obj}，这是稳定的个人兴趣。'
                        fact('likes', obj)
                elif slot == 1:
                    obj = ('绿茶', '红茶', '乌龙茶')[block]
                    text = f'我长期喜欢{obj}，以后也可以保留这项偏好。'
                    fact('likes', obj)
                elif slot == 2:
                    obj = ('蓝色自行车', '绿色头盔', '黑色笔记本')[block]
                    text = f'我拥有一个{obj}。这是我自己的物品。'
                    fact('owns', obj, [[user, 'has', obj]])
                elif slot == 3:
                    obj = ('蓝色自行车', '绿色头盔', '黑色笔记本')[block]
                    person = ('林岚', '陈星', '周宁')[block]
                    text = f'刚才提到的那件物品，是{person}送给我的。'
                    fact('given_by', person, [[obj, 'gifted_by', person]], subject=obj,
                         sources=[[f'e{i}', key]])
                    case['facts'][-1]['evidence'] = {f'e{i}': obj, key: person}
                elif slot in (4, 5):
                    city = ('杭州', '苏州', '南京', '无锡', '合肥', '芜湖')[block*2+slot-4]
                    text = (f'从这条消息开始，我现在住在{city}，这是最新的常住地址。'
                            if slot == 4 and block == 0 else
                            f'更正常住地址：从这条消息开始我搬到{city}居住，之前的住处已经失效。')
                    row = fact('lives_in', city)
                    if user in previous:
                        previous[user]['replaced_by'] = row['id']
                    previous[user] = row
                elif slot == 6:
                    task = ('提交报销单', '领取快递', '预约会议室')[block]
                    text = f'今天下午需要{task}，这是临时待办，办完即结束，不作为长期事实。'
                    case['working'].append(dict(id=f'w{i+1}', contents=[text, f'今天下午需要{task}',
                        f'今天下午{task}', task], kinds=['task'], sources=[[key]], at=key))
                else:
                    text = '好的，谢谢。此条没有新增个人事实，也没有新的任务或状态。'
                event['text'] = text + padding
                case['events'].append(event)
            if family == 'longinput':
                # Unique procedural sections, rather than repeated facts or padding.
                positions = [2] if size == 'short' else [2, 16]
                for position in positions:
                    event = case['events'][position]
                    target = (24 if size == 'short' else 32) * 1024
                    sections = []
                    for n in range(1, 500):
                        sections.append(f'阅读练习第{n}节：在虚构的流程图中，入口{n}连接检查点{n+1}，'
                            f'遇到分支时先比较编号{n+2}和编号{n+3}。本节用于理解流程顺序，'
                            '不描述用户经历、所有物或真实待办。检查完毕后沿图示方向继续阅读。')
                        if len('\n'.join(sections).encode('utf-8')) >= target:
                            break
                    event['text'] += '\n以下是虚构阅读练习材料，不是我的个人信息：\n' + '\n'.join(sections)
                    event['text'] += '\n材料结束。' + event['text'].split('。', 1)[0] + '。'
                    middle = f'我长期喜欢合成音乐类型{position}。'
                    end = f'我拥有合成书签{position}。'
                    split = event['text'].index('\n阅读练习第40节')
                    event['text'] = event['text'][:split] + '\n' + middle + event['text'][split:] + '\n' + end
                    for label, predicate, obj, quote in (
                        ('middle', 'likes', f'合成音乐类型{position}', middle),
                        ('end', 'owns', f'合成书签{position}', end)):
                        case['facts'].append(dict(id=f'{event["id"]}-{label}',
                            variants=[['u', predicate, obj]], sources=[[event['id']]], at=event['id'],
                            evidence={event['id']: quote}))
            if family == 'inflight':
                case['controls'].append(dict(delay=2, user='u', session='s', kind='flush'))
            if family == 'isolation':
                case['controls'].extend(dict(delay=count*.125, user=u, session=None, kind='flush') for u in 'uv')
            cases.append(case)
    return dict(version=1, kind='buffered_replay', cases=cases)


def load_suite(path=SUITE):
    from extraction_quality import load_suite as load_gold
    value = load_gold(path)
    if value.get('kind') != 'buffered_replay':
        raise ValueError('Not a continuous replay suite')
    for case in value['cases']:
        events = case['events']
        if (len(events) not in (8, 24) or any(type(e['delay']) not in (int, float)
                or not math.isfinite(e['delay']) or e['delay'] < 0 for e in events)
                or [e['delay'] for e in events] != sorted(e['delay'] for e in events)):
            raise ValueError('Invalid replay arrival schedule')
        for control in case.get('controls', []):
            if (control['kind'] != 'flush' or control['user'] not in {e['user'] for e in events}
                    or type(control['delay']) not in (int, float) or not math.isfinite(control['delay'])
                    or control['delay'] < 0):
                raise ValueError('Invalid replay control')
    return value


def replay(case, call, *, clock=time.perf_counter, wait=time.sleep):
    """Offer a fixed stream; consume each user's commands in FIFO order.

    `call` receives a command and its shared observation. It must report failures;
    consumers continue draining so a failed request cannot deadlock the producer.
    """
    commands = [dict(kind='event', delay=e['delay'], user=e['user'], event=e) for e in case['events']]
    commands += case.get('controls', [])
    commands.sort(key=lambda c: c['delay'])
    users = sorted({c['user'] for c in commands})
    queues = {u: queue.Queue() for u in users}
    observations = []
    started = clock()
    def consume(user):
        while (pair := queues[user].get()) is not None:
            command, row = pair
            row['entered_at'] = clock() - started
            try:
                call(command, row)
                row['status'] = 'completed'
            except Exception as exc:
                row.update(status='failed', error_type=type(exc).__name__)
            finally:
                row['returned_at'] = clock() - started
    with ThreadPoolExecutor(max_workers=len(users), thread_name_prefix='replay-ingress') as pool:
        futures = [pool.submit(consume, u) for u in users]
        for command in commands:
            remaining = started + command['delay'] - clock()
            if remaining > 0:
                wait(remaining)
            row = dict(kind=command['kind'], user=command['user'], planned_at=command['delay'],
                       generated_at=clock() - started)
            if command['kind'] == 'event':
                row['event'] = command['event']['id']
            observations.append(row)
            queues[command['user']].put((command, row))
        for pending in queues.values():
            pending.put(None)
        for future in futures:
            future.result()
    return observations
