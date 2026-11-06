"""规则版仲裁器：从积压事件里挑下一个值得回应的（注意力分配）。

规则（README 路线图中未来会继续被"节目效果"学习增强）：
- 高能事件（礼物 / 死亡 / 收获，priority >= PRIO_GIFT）立刻回应
- 普通事件（弹幕 / 游戏日常）共用冷却，冷却期内短暂保留、过期再丢弃
- 相邻普通弹幕可聚合进同一轮上下文，重复事件会去重
- 太久没说话就主动找话（idle 事件）
"""
from __future__ import annotations

import time

from .events import PRIO_GIFT, PRIO_IDLE, Event


class Arbiter:
    def __init__(
        self,
        danmaku_cooldown_sec: float = 8.0,
        idle_comment_sec: float = 45.0,
        *,
        max_pending: int = 200,
        normal_max_age_sec: float = 30.0,
        aggregate_limit: int = 5,
    ):
        self.cooldown = danmaku_cooldown_sec
        self.idle_after = idle_comment_sec
        self.max_pending = max(10, max_pending)
        self.normal_max_age = max(5.0, normal_max_age_sec)
        self.aggregate_limit = max(1, aggregate_limit)
        self._pending: list[Event] = []
        self._last_normal_reply = 0.0
        self._last_reply = time.time()
        self.expired = 0
        self.dropped = 0
        self.aggregated = 0
        self._last_served: dict[str, float] = {}

    @staticmethod
    def _viewer(event):
        return event.viewer_key or event.user or ''

    def push(self, event: Event) -> None:
        host_question = bool(getattr(event, 'host_question_id', None))
        if host_question:
            # The host selecting a saved card replaces the still-pending original;
            # it must not be swallowed by ordinary chat deduplication.
            self._pending = [old for old in self._pending if not (
                not getattr(old, 'host_question_id', None) and old.source == event.source
                and old.kind == event.kind and self._viewer(old) == self._viewer(event)
                and old.text == event.text)]
        if event.task_notice:
            self._pending = [old for old in self._pending
                             if not (old.task_notice and old.task_id == event.task_id)]
        if not host_question and event.kind == "chat" and any(
            old.source == event.source
            and old.kind == event.kind
            and self._viewer(old) == self._viewer(event)
            and old.text == event.text
            for old in self._pending[-20:]
        ):
            self.dropped += 1
            return
        self._pending.append(event)
        self._trim_pending()

    def discard_unforced(self) -> None:
        """进入手动测试模式时清掉尚未处理的自动事件。"""
        self._pending = [event for event in self._pending if event.forced or event.task_id]

    def discard_before(self, turn_id: int) -> list[Event]:
        removed = [event for event in self._pending if event.ui_id < turn_id]
        self._pending = [event for event in self._pending if event.ui_id >= turn_id]
        self.dropped += len(removed)
        return removed

    def pick(self, allow_idle: bool = True, *, allow_automatic: bool = True) -> Event | None:
        now = time.time()
        self._prune(now)
        if self._pending:
            forced = next((event for event in self._pending if event.forced), None)
            if forced is not None:
                self._pending.remove(forced)
                return self._mark(forced)
            if not allow_automatic:
                return None
            urgent = [event for event in self._pending if event.priority >= PRIO_GIFT]
            if urgent:
                best = max(urgent, key=lambda e: (e.priority, -e.ts))
                self._pending.remove(best)
                return self._mark(best)
            notices = [event for event in self._pending if event.task_notice]
            if notices and now - self._last_normal_reply >= self.cooldown:
                best = min(notices, key=lambda event: event.ts)
                self._pending.remove(best)
                return self._mark(best)
            if now - self._last_normal_reply >= self.cooldown:
                best = max(self._pending, key=lambda e: (e.priority, e.ts))
                if best.source == 'danmaku' and best.kind == 'chat':
                    chats = [e for e in self._pending if e.source == 'danmaku' and e.kind == 'chat']
                    best = min(chats, key=lambda e: (self._last_served.get(self._viewer(e), 0), e.ts))
                self._pending.remove(best)
                self._attach_related_chat(best)
                return self._mark(best)
        if allow_idle and allow_automatic and now - self._last_reply >= self.idle_after:
            return self._mark(Event("system", "idle", "直播间冷场了", priority=PRIO_IDLE))
        return None

    def remove_turn(self, turn_id: int) -> None:
        self._pending = [event for event in self._pending if getattr(event, 'ui_id', None) != turn_id]

    def pending_turn_ids(self) -> set[int]:
        return {event.ui_id for event in self._pending if hasattr(event, 'ui_id')}

    def has_urgent(self, *, above_priority: int = 0) -> bool:
        return any(
            event.forced or (event.priority >= PRIO_GIFT and event.priority > above_priority)
            for event in self._pending
        )

    def summary(self) -> dict[str, int]:
        return {
            "pending": len(self._pending),
            "expired": self.expired,
            "dropped": self.dropped,
            "aggregated": self.aggregated,
        }

    def _mark(self, event: Event) -> Event:
        now = time.time()
        self._last_normal_reply = now
        self._last_reply = now
        if event.source == 'danmaku' and event.kind == 'chat':
            key = self._viewer(event)
            self._last_served.pop(key, None)
            self._last_served[key] = now
            if len(self._last_served) > 500:
                self._last_served.pop(next(iter(self._last_served)))
        return event

    def _attach_related_chat(self, selected: Event) -> None:
        if getattr(selected, 'host_question_id', None):
            return
        if selected.source != "danmaku" or selected.kind != "chat" or selected.task_id or selected.agent_reply or selected.task_intent_pending:
            return
        candidates = [
            event
            for event in self._pending
            if event.source == "danmaku" and event.kind == "chat" and not event.forced
            and not event.task_id and not event.agent_reply
            and not event.task_intent_pending
            and self._viewer(event) == self._viewer(selected)
        ]
        candidates.sort(key=lambda event: event.ts, reverse=True)
        chosen = candidates[: self.aggregate_limit]
        if not chosen:
            return
        for event in chosen:
            self._pending.remove(event)
        selected.related_messages.extend(
            {"source": event.source, "kind": event.kind, "user": event.user,
             "viewer_key": event.viewer_key, "text": event.text, "ts": event.ts,
             "ui_id": getattr(event, "ui_id", None)}
            for event in reversed(chosen)
        )
        self.aggregated += len(chosen)

    def _prune(self, now: float) -> None:
        kept: list[Event] = []
        for event in self._pending:
            max_age = 300.0 if event.task_notice else 180.0 if event.priority >= PRIO_GIFT or event.forced else self.normal_max_age
            if now - event.ts > max_age:
                self.expired += 1
            else:
                kept.append(event)
        self._pending = kept

    def _trim_pending(self) -> None:
        while len(self._pending) > self.max_pending:
            candidates = [event for event in self._pending if not event.forced and not event.task_notice]
            if not candidates:
                break
            victim = min(candidates, key=lambda event: (event.priority, event.ts))
            self._pending.remove(victim)
            self.dropped += 1
