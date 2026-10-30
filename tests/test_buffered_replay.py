"""Replay offered load is independent of synchronous model latency."""
import importlib
import json
from pathlib import Path
import threading

import pytest


@pytest.fixture
def replay_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'eval'))
    return importlib.import_module('buffered_replay')


def test_frozen_matrix_has_arrivals_gold_and_unique_long_sections(replay_module):
    suite = replay_module.load_suite()
    assert suite == replay_module.build_suite()
    assert len(suite['cases']) == 12
    assert {len(c['events']) for c in suite['cases']} == {8, 24}
    assert sum(len(c['events']) for c in suite['cases']) == 192
    for case in suite['cases']:
        for event in case['events']:
            if len(event['text'].encode()) < 24000:
                assert 100 <= len(event['text']) <= 300
    long_cases = [c for c in suite['cases'] if c['category'] == 'longinput']
    assert [sum(len(e['text'].encode()) > 24000 for e in c['events']) for c in long_cases] == [1, 2]
    assert set(replay_module.PILOT) <= {c['id'] for c in suite['cases']}


def test_producer_keeps_arriving_while_namespace_consumer_is_blocked(replay_module):
    stamp, produced = [0.0], threading.Event()
    def advance(seconds):
        stamp[0] += seconds
        if stamp[0] == 2:
            produced.set()
    observed = []
    def call(command, row):
        if command['event']['id'] == 'e0':
            assert produced.wait(2), 'producer waited for the first remember response'
        observed.append(command['event']['id'])
    case = dict(events=[dict(id=f'e{i}', user='u', delay=i) for i in range(3)])
    rows = replay_module.replay(case, call, clock=lambda: stamp[0], wait=advance)
    assert observed == ['e0', 'e1', 'e2']
    assert [r['generated_at'] for r in rows] == [0, 1, 2]
    assert rows[1]['entered_at'] >= 2


def test_failure_does_not_drop_later_requests_or_cross_namespace_fifo(replay_module):
    entered, other = threading.Event(), threading.Event()
    order = []
    def call(command, row):
        key = command['event']['id']
        if key == 'u1':
            entered.set()
            assert other.wait(2)
            raise ValueError('synthetic request failure')
        if key == 'v1':
            assert entered.wait(2)
            other.set()
        order.append(key)
    case = dict(events=[dict(id=k, user=k[0], delay=0) for k in ('u1', 'u2', 'v1', 'v2')])
    rows = replay_module.replay(case, call)
    assert rows[0]['status'] == 'failed' and rows[1]['status'] == 'completed'
    assert order.index('v1') < order.index('v2')
    assert len(rows) == 4


def test_controls_follow_preceding_events_without_stalling_producer(replay_module):
    order = []
    case = dict(events=[dict(id='e1', user='u', delay=0), dict(id='e2', user='u', delay=1)],
                controls=[dict(kind='flush', user='u', session='s', delay=.5)])
    stamp = [0]
    rows = replay_module.replay(case, lambda c, r: order.append(c['kind']),
        clock=lambda: stamp[0], wait=lambda d: stamp.__setitem__(0, stamp[0]+d))
    assert order == ['event', 'flush', 'event']
    assert rows[2]['planned_at'] == 1


def test_bad_arrival_schedule_is_rejected(replay_module, tmp_path):
    suite = replay_module.build_suite()
    suite['cases'][0]['events'][0]['delay'] = -1
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(suite), encoding='utf-8')
    with pytest.raises(ValueError, match='arrival schedule'):
        replay_module.load_suite(path)
