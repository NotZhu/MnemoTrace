"""Wake and rotate namespaces; all durable work belongs to the pipeline ledger."""
from collections import OrderedDict
import logging
import threading
import time


class PipelineWorker:
    def __init__(self, service, *, clock=time.time, monotonic=time.monotonic):
        self.service, self.clock, self.monotonic = service, clock, monotonic
        self.condition = threading.Condition()
        self.users = OrderedDict()
        self.revision = 0
        self.abort = threading.Event()
        self.stopping = False
        self.recovery_error = False
        self.last_scan = 0
        self.thread = threading.Thread(target=self.consume, name='mnemotrace-pipeline', daemon=True)
        self.recover()

    def notify(self, user):
        with self.condition:
            self.revision += 1
            self.users[user] = self.revision
            self.condition.notify_all()

    def scan(self):
        from .store.persist import discover_processing_namespaces
        errors = []
        found = discover_processing_namespaces(self.service.data_dir, on_error=errors.append)
        for user, active in found:
            if active:
                self.notify(user)
        self.last_scan = self.monotonic()
        self.recovery_error = bool(errors)
        if errors:
            self.service.metrics.count('pipeline_recovery_failed')
            logging.getLogger(__name__).warning('Pipeline recovery failed; inspect namespace storage')
        return [user for user, _ in found]

    def status(self, user, session_id=None, ids=None):
        return self.service._processing_status(user, session_id, ids)

    def recover(self):
        try:
            self.scan()
        except Exception:
            self.recovery_error = True
            self.last_scan = self.monotonic()
            self.service.metrics.count('pipeline_recovery_failed')
            logging.getLogger(__name__).warning('Pipeline recovery failed; inspect namespace storage')

    def run_one(self, user):
        from ._pipeline import run_one
        return run_one(self.service, user)

    def flush(self, user, session_id=None, timeout=30, retry_failed=False, boundary=None):
        from ._pipeline import flush
        return flush(self.service, user, session_id, timeout, retry_failed, boundary)

    def consume(self):
        while not self.abort.is_set():
            if self.monotonic() - self.last_scan >= 5:
                self.recover()
            with self.condition:
                users = list(self.users.items())
            progress = False
            for user, revision in users:
                try:
                    progress |= self.run_one(user)
                    active = any(self.status(user)['counts'][state] for state in ('pending', 'running', 'retry_wait'))
                    with self.condition:
                        if self.users.get(user) == revision and not active:
                            self.users.pop(user, None)
                        elif user in self.users:
                            self.users.move_to_end(user)
                except Exception:
                    self.service.metrics.count('pipeline_failed')
                    # 带堆栈记录：提交中途失败可能留下未提交的集合文件，只报一句
                    # “work remains recoverable” 会让这类问题无法定位。
                    logging.getLogger(__name__).exception(
                        'Pipeline step failed; inspect the namespace store before retrying')
                if self.abort.is_set():
                    break
            if not progress:
                with self.condition:
                    if self.stopping:
                        break
                    self.condition.wait(.25)

    def close(self, timeout=30):
        self.stopping = True
        with self.condition:
            self.condition.notify_all()
        if self.thread.ident is not None and threading.current_thread() is not self.thread and not self.service._frames():
            self.thread.join(timeout)
        self.abort.set()
