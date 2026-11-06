"""Advance a host-opened tea party only when the shared stage is available."""
from __future__ import annotations

import asyncio
import sqlite3

from .safety import is_crisis


def holds_audience_event(tarot, event):
    """Let host controls and urgent support through, keep other chat off-table."""
    if (tarot is None or event.source != 'danmaku' or event.kind not in {'chat', 'gift'}
            or event.forced or is_crisis(event.text)):
        return False
    holds = getattr(tarot, 'holds_chat', None)
    return bool(holds and holds())


class TarotHost:
    def __init__(self, tarot, register, *, available, interval=0.5):
        self.tarot, self.register, self.available = tarot, register, available
        self.interval = interval
        self.last_error = ''

    async def tick(self):
        if not self.available():
            return None
        event = self.tarot.next_event()
        if event is None:
            return None
        try:
            await self.register(event)
        except asyncio.CancelledError:
            self.tarot.mark_result(event, 'interrupted', error='主持已暂停，本阶段未完成，请从原阶段继续。')
            raise
        except Exception:
            self.tarot.mark_result(event, 'failed', error='本阶段未能进入播放流程，请手动重试。')
            self.last_error = '塔罗主持未完成，请在牌桌检查并重试。'
            return None
        self.last_error = ''
        return event

    async def run(self):
        while True:
            try:
                await self.tick()
            except (OSError, sqlite3.Error, ValueError):
                # State transitions are durable; never resubmit a claimed turn
                # merely because a later read or scheduler operation failed.
                notice = '塔罗主持状态暂不可用，请在牌桌检查。'
                if self.last_error != notice:
                    print('[tarot] ' + notice)
                self.last_error = notice
            await asyncio.sleep(self.interval)
