"""Supervise named, long-running workers without leaking exception payloads."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable


class RuntimeSupervisor:
    def __init__(self, *, max_restarts=3, base_delay=1.0, max_delay=30.0,
                 clock=time.time):
        if any(not math.isfinite(value) or value < 0
               for value in (max_restarts, base_delay, max_delay)):
            raise ValueError('恢复次数与退避时间不能小于零')
        self.max_restarts = int(max_restarts)
        self.base_delay, self.max_delay = float(base_delay), float(max_delay)
        self.clock = clock
        self._workers: dict[str, dict] = {}
        self._closing = False

    def start(self, name: str, factory: Callable[[], Awaitable], *,
              restart=True, max_restarts=None, critical=True) -> asyncio.Task:
        if self._closing:
            raise RuntimeError('运行监督器已关闭')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('后台任务名称无效')
        previous = self._workers.get(name)
        if previous and not previous['task'].done():
            raise ValueError('同名后台任务正在运行')
        budget = self.max_restarts if max_restarts is None else int(max_restarts)
        if budget < 0:
            raise ValueError('恢复次数不能小于零')
        record = dict(name=name, state='running', restarts=0, last_error=None,
                      started_at=self.clock(), stopped_at=None, retry_at=None,
                      stopping=False, critical=bool(critical))
        self._workers[name] = record
        task = asyncio.create_task(self._run(record, factory, restart, budget),
                                   name='supervisor:' + name)
        record['task'] = task
        task.add_done_callback(lambda done: self._finished(record, done))
        return task

    async def _run(self, record, factory, restart, budget):
        try:
            while not self._closing and not record['stopping']:
                record.update(state='running', retry_at=None)
                try:
                    await factory()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Error messages can contain URLs, tokens and request bodies.
                    # Only a bounded class name is suitable for the host status UI.
                    kind = type(error).__name__
                    kind = ''.join(c for c in kind if c.isalnum() or c == '_')[:64]
                    record['last_error'] = f'{kind or "Exception"}：后台任务异常退出'
                else:
                    if not restart:
                        record['state'] = 'stopped'
                        return
                    record['last_error'] = 'UnexpectedExit：后台任务提前结束'
                if self._closing or record['stopping']:
                    break
                if not restart or record['restarts'] >= budget:
                    record['state'] = 'failed'
                    return
                delay = min(self.max_delay, self.base_delay * 2 ** min(record['restarts'], 30))
                record['restarts'] += 1
                record.update(state='retrying', retry_at=self.clock() + delay)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            record['state'] = 'stopped'
            raise
        finally:
            record['retry_at'] = None
            record['stopped_at'] = self.clock()
            if record['stopping'] or self._closing:
                record['state'] = 'stopped'

    def _finished(self, record, task):
        # Cancellation before the coroutine's first instruction skips its finally.
        if task.cancelled():
            record.update(state='stopped', retry_at=None, stopped_at=self.clock())
        else:
            error = task.exception()  # Always retrieve unexpected wrapper failures.
            if error is not None:
                record.update(state='failed', retry_at=None, stopped_at=self.clock(),
                              last_error='SupervisorError：后台任务监督器异常')

    async def stop(self, name: str):
        record = self._workers.get(name)
        if record is None:
            return
        record['stopping'] = True
        task = record['task']
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        record.update(state='stopped', retry_at=None, stopped_at=self.clock())

    async def close(self):
        self._closing = True
        await asyncio.gather(*(self.stop(name) for name in tuple(self._workers)))

    def summary(self) -> dict:
        fields = ('state', 'critical', 'restarts', 'last_error', 'started_at', 'stopped_at', 'retry_at')
        workers = {name: {key: item[key] for key in fields}
                   for name, item in self._workers.items()}
        return {'workers': workers, 'closing': self._closing,
                'ready': bool(workers) and not self._closing and all(
                    item['state'] == 'running' for item in workers.values() if item['critical']),
                'warnings': [name for name, item in workers.items()
                             if not item['critical'] and item['state'] != 'running']}
