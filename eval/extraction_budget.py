"""Shared, fail-closed request budget for sequential evaluation subprocesses."""
from __future__ import annotations

import json
from pathlib import Path
import time


class BudgetStopped(RuntimeError):
    pass


class Budget:
    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def create(cls, path, *, calls=2000, tokens=3_000_000, seconds=3600):
        if any(type(v) is not int or v < 1 for v in (calls, tokens, seconds)):
            raise ValueError('Budget limits must be positive integers')
        with Path(path).open('x', encoding='utf-8') as stream:
            json.dump(dict(calls=0, tokens=0, missing_usage=0, inflight=0, started=time.time(),
                           limits=dict(calls=calls, tokens=tokens, seconds=seconds)), stream)
        return cls(path)

    def _change(self, function):
        from mnemotrace._namespace_lock import namespace_lock
        with namespace_lock(str(self.path) + '.lock').file_lock():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            result = function(data)
            pending = self.path.with_suffix('.pending')
            pending.write_text(json.dumps(data), encoding='utf-8')
            pending.replace(self.path)
            return result

    def claim(self):
        def update(data):
            limits = data['limits']
            if (data['missing_usage'] or data['calls'] >= limits['calls'] or data['tokens'] >= limits['tokens']
                    or time.time() - data['started'] >= limits['seconds']):
                raise BudgetStopped('Evaluation budget stopped')
            data['calls'] += 1
            data['inflight'] += 1
        self._change(update)

    def finish(self, usage):
        def update(data):
            data['inflight'] -= 1
            if usage is None or 'total_tokens' not in usage:
                data['missing_usage'] += 1
            else:
                data['tokens'] += usage['total_tokens']
        self._change(update)
