"""Bounded, ordered delivery that isolates slow browser connections."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Awaitable, Callable


class SocketOutbox:
    def __init__(self, send: Callable[[str], Awaitable[None]],
                 on_failure: Callable[[], Awaitable[None]], *, capacity: int = 128,
                 timeout: float = 2.0):
        self.send = send
        self.on_failure = on_failure
        self.capacity = max(1, capacity)
        self.timeout = timeout
        self.pending: deque[tuple[str, str | None]] = deque()
        self.wake = asyncio.Event()
        self.closed = False
        self.failed = False
        self.coalesced = 0
        self.worker = asyncio.create_task(self._run(), name='browser-outbox')

    def offer(self, text: str, *, state_key: str | None = None) -> bool:
        if self.closed or self.failed:
            return False
        if state_key is not None:
            for item in tuple(self.pending):
                if item[1] == state_key:
                    self.pending.remove(item)
                    self.coalesced += 1
                    break
        if len(self.pending) >= self.capacity:
            disposable = next((item for item in self.pending if item[1] is not None), None)
            if disposable is not None:
                self.pending.remove(disposable)
                self.coalesced += 1
            else:
                # Audio, controls and chat are never silently dropped. Reconnect
                # makes the failure explicit and performs normal playback cleanup.
                self.failed = True
                self.wake.set()
                return False
        self.pending.append((text, state_key))
        self.wake.set()
        return True

    async def _run(self) -> None:
        try:
            while not self.closed:
                if self.failed:
                    break
                if not self.pending:
                    self.wake.clear()
                    await self.wake.wait()
                    continue
                text, _ = self.pending.popleft()
                await asyncio.wait_for(self.send(text), timeout=self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
        finally:
            if self.failed and not self.closed:
                self.closed = True
                self.pending.clear()
                await self.on_failure()

    async def close(self) -> None:
        self.closed = True
        self.pending.clear()
        self.wake.set()
        if self.worker is not asyncio.current_task():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
