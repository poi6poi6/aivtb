"""后端语音播控：单一播放时间线、优先级插播、过期和超时处理。"""
from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import PlaybackConfig


@dataclass
class SpeechItem:
    id: int
    url: str
    text: str
    emotion: str
    actions: list[str]
    event: dict[str, Any]
    priority: int
    created_at: float = field(default_factory=time.time)
    turn_id: int | None = None
    segment_index: int = 0
    ready_at: float = field(default_factory=time.perf_counter)
    played_seconds: float = 0.0
    expiry_extension: float = 0.0

    def message(self) -> dict[str, Any]:
        return {
            "type": "audio",
            "id": self.id,
            "url": self.url,
            "text": self.text,
            "emotion": self.emotion,
            "actions": list(self.actions),
            "event": dict(self.event),
            "priority": self.priority,
            "turn_id": self.turn_id if self.turn_id is not None else self.id,
            "segment_index": self.segment_index,
        }


class SpeechController:
    """所有方法在 aiohttp 主事件循环调用，因此状态变更无需线程锁。"""

    def __init__(self, cfg: PlaybackConfig, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._clock = clock
        self.current: SpeechItem | None = None
        self.queue: list[SpeechItem] = []
        self.state = "idle"  # idle | dispatched | playing
        self.dispatched_at = 0.0
        self.started_at = 0.0
        self.dropped = 0
        self.last_reason = ""
        self.on_delivery: Callable[[SpeechItem, bool], None] | None = None
        self.item_valid: Callable[[SpeechItem], bool] | None = None
        self.on_dispatch: Callable[[SpeechItem], None] | None = None
        self.on_state: Callable[[SpeechItem, str, str], None] | None = None
        self.paused = False
        self.paused_at = 0.0
        self._resume_state = "idle"
        # A reply already admitted to playback remains fresh as a whole. Its
        # later segments can wait for capacity without aging out mid-sentence.
        self._continuing_turn: int | None = None
        self.generation = 0

    def has_capacity(self, priority: int) -> bool:
        """The synthesis worker waits here instead of evicting queued segments."""
        return (
            self.current is None and not self.queue
            or len(self.queue) < max(0, self.cfg.max_queue)
            or self.current is not None
            and priority >= self.cfg.interrupt_priority
            and priority > self.current.priority
        )

    def turn_is_stale(self, turn_id: int | None, created_at: float,
                      expiry_extension: float = 0.0) -> bool:
        if self.paused or turn_id is not None and turn_id == self._continuing_turn:
            return False
        return self._clock() - created_at - expiry_extension > max(5.0, self.cfg.max_age_sec)

    def set_paused(self, paused: bool) -> list[dict[str, Any]]:
        if self.paused == paused:
            return [self.status_message()]
        now = self._clock()
        self.paused = paused
        if paused:
            self.paused_at = now
            self._resume_state = self.state
            self.state = "paused"
        else:
            elapsed = max(0, now - self.paused_at)
            self.started_at += elapsed
            self.dispatched_at += elapsed
            for item in self.queue + ([self.current] if self.current else []):
                item.expiry_extension += elapsed
            self.state = self._resume_state if self.current else "idle"
        messages = []
        if not paused and self.current and self.item_valid and not self.item_valid(self.current):
            return self.skip_current()
        if self.current:
            messages.append({"type": "audio_control", "action": "pause" if paused else "resume", "id": self.current.id})
        elif not paused:
            messages.extend(self._dispatch_next(now))
        messages.append(self.status_message())
        return messages

    def audio_progress(self, audio_id: int, seconds: object) -> None:
        if self.current is None or self.current.id != audio_id or not (self.state == "playing" or self.paused and self._resume_state == "playing"):
            return
        try:
            value = float(seconds)
        except (ValueError, TypeError):
            return
        if math.isfinite(value) and 0 <= value <= self.cfg.max_play_sec:
            self.current.played_seconds = max(self.current.played_seconds, value)

    def _delivered(self, completed: bool = False) -> None:
        if self.current and self.on_delivery and (self.state == "playing" or self.paused and self._resume_state == "playing"):
            self.on_delivery(self.current, completed)

    def enqueue(self, item: SpeechItem) -> list[dict[str, Any]]:
        now = self._clock()
        messages: list[dict[str, Any]] = []
        self._expire_queue(now)
        if self._is_stale(item, now):
            if self.on_state:
                self.on_state(item, 'failed', '语音生成完成时事件已过时')
            self._drop("语音生成完成时事件已过时")
            return [self.status_message()]

        if self.current is None and not self.queue:
            messages.append(self._dispatch(item, now))
        elif (
            self.current is not None
            and
            item.priority >= self.cfg.interrupt_priority
            and item.priority > self.current.priority
        ):
            interrupted_id = self.current.id
            if self.on_state:
                self.on_state(self.current, 'cancelled', '被高优先级语音中断')
            self._delivered()
            self._drop(f"被高优先级语音#{item.id}中断")
            self.current = None
            self.state = "idle"
            messages.append({"type": "audio_control", "action": "stop", "id": interrupted_id})
            messages.append(self._dispatch(item, now))
        else:
            self.queue.append(item)
            self.queue.sort(key=lambda queued: (-queued.priority, queued.created_at))
            self._trim_queue()
            if self.current is None:
                messages.extend(self._dispatch_next(now))
        messages.append(self.status_message())
        return messages

    def audio_started(self, event_id: int) -> list[dict[str, Any]]:
        if self.current is None or self.current.id != event_id:
            return []
        if self.item_valid and not self.item_valid(self.current):
            return self.skip_current()
        if self.has_started:
            return []
        # Resume adds the entire pause interval to this clock. A receipt that
        # races the pause command therefore starts at the frozen pause instant.
        self.started_at = self.paused_at if self.paused else self._clock()
        self.last_reason = ""
        if self.paused:
            self._resume_state = "playing"
        else:
            self.state = "playing"
        if self.on_state:
            self.on_state(self.current, 'playing', '')
        if self.paused:
            return [{"type": "audio_control", "action": "pause", "id": event_id}, self.status_message()]
        return [self.status_message()]

    @property
    def has_started(self) -> bool:
        return self.current is not None and (
            self.state == "playing" or self.paused and self._resume_state == "playing"
        )

    def audio_finished(self, event_id: int, reason: str = "ended") -> list[dict[str, Any]]:
        if self.current is None or self.current.id != event_id:
            return []
        self.last_reason = reason[:40]
        if self.on_state:
            self.on_state(self.current, 'played' if reason == 'ended' else 'failed', reason[:120])
        self._delivered(reason == "ended")
        self.current = None
        self.state = "idle"
        return self._dispatch_next(self._clock())

    def skip_current(self) -> list[dict[str, Any]]:
        if self.current and self.on_state:
            self.on_state(self.current, 'cancelled', '用户跳过')
        if self.current is None:
            return [self.status_message()]
        event_id = self.current.id
        self.last_reason = "手动跳过"
        self._delivered()
        self.current = None
        self.state = "idle"
        messages = [{"type": "audio_control", "action": "stop", "id": event_id}]
        messages.extend(self._dispatch_next(self._clock()))
        return messages

    def clear(self) -> list[dict[str, Any]]:
        self.generation += 1
        self._continuing_turn = None
        if self.on_state:
            for item in self.queue + ([self.current] if self.current else []):
                self.on_state(item, 'cancelled', '用户清空播放')
        event_id = self.current.id if self.current else None
        removed = len(self.queue) + (1 if self.current else 0)
        self.queue.clear()
        self._delivered()
        self.current = None
        self.state = "idle"
        self.paused = False
        self.dropped += removed
        self.last_reason = "手动清空队列"
        messages: list[dict[str, Any]] = []
        if event_id is not None:
            messages.append({"type": "audio_control", "action": "clear", "id": event_id})
        messages.append(self.status_message())
        return messages

    def cancel_turn(self, turn_id: int, reason: str = "回复被高优先级事件中断") -> list[dict[str, Any]]:
        """Remove every current or queued speech segment belonging to a turn."""

        if self._continuing_turn == turn_id:
            self._continuing_turn = None
        messages: list[dict[str, Any]] = []
        kept: list[SpeechItem] = []
        removed = 0
        for item in self.queue:
            owner = item.turn_id if item.turn_id is not None else item.id
            if owner == turn_id:
                if self.on_state:
                    self.on_state(item, 'cancelled', reason)
                removed += 1
            else:
                kept.append(item)
        self.queue = kept
        if self.current is not None:
            owner = self.current.turn_id if self.current.turn_id is not None else self.current.id
            if owner == turn_id:
                if self.on_state:
                    self.on_state(self.current, 'cancelled', reason)
                messages.append({"type": "audio_control", "action": "stop", "id": self.current.id})
                self._delivered()
                self.current = None
                self.state = "idle"
                removed += 1
        if removed:
            self.dropped += removed
            self.last_reason = reason
        if self.current is None:
            messages.extend(self._dispatch_next(self._clock()))
        elif removed:
            messages.append(self.status_message())
        return messages

    def tick(self) -> list[dict[str, Any]]:
        if self.paused:
            return []
        now = self._clock()
        expired = self._expire_queue(now)
        messages: list[dict[str, Any]] = []
        timed_out = False
        if self.current is not None and self.state == "dispatched":
            timed_out = now - self.dispatched_at >= max(3.0, self.cfg.start_timeout_sec)
            reason = "等待浏览器播放超时"
        elif self.current is not None and self.state == "playing":
            timed_out = now - self.started_at >= max(10.0, self.cfg.max_play_sec)
            reason = "单条语音播放超时"
        else:
            reason = ""
        if timed_out and self.current is not None:
            if self.on_state:
                self.on_state(self.current, 'failed', reason)
            event_id = self.current.id
            self._drop(reason)
            self._delivered()
            self.current = None
            self.state = "idle"
            messages.append({"type": "audio_control", "action": "stop", "id": event_id})
            messages.extend(self._dispatch_next(now))
        elif expired:
            messages.append(self.status_message())
        return messages

    def redispatch_current(self) -> list[dict[str, Any]]:
        """音频客户端重连时从头重播尚未完成的当前语音。"""
        if self.current is None:
            return [self.status_message()]
        if self.item_valid and not self.item_valid(self.current):
            return self.skip_current()
        self.state = "dispatched"
        self.dispatched_at = self._clock()
        self.started_at = 0.0
        if self.paused:
            self._resume_state = "dispatched"
            self.state = "paused"
        return [{**self.current.message(), "paused": self.paused}, self.status_message()]

    def summary(self) -> dict[str, Any]:
        return {
            "state": "paused" if self.paused else self.state,
            "paused": self.paused,
            "current_id": self.current.id if self.current else None,
            "current_text": self.current.text[:36] if self.current else "",
            "current_priority": self.current.priority if self.current else None,
            "current_turn_id": (
                self.current.turn_id if self.current and self.current.turn_id is not None
                else self.current.id if self.current else None
            ),
            "queued": len(self.queue),
            "queued_ids": [item.id for item in self.queue],
            "dropped": self.dropped,
            "last_reason": self.last_reason,
        }

    def status_message(self) -> dict[str, Any]:
        return {"type": "playback", **self.summary()}

    def _dispatch(self, item: SpeechItem, now: float) -> dict[str, Any]:
        if self.on_dispatch:
            self.on_dispatch(item)
        self.current = item
        self._continuing_turn = item.turn_id
        self.state = "dispatched"
        self.dispatched_at = now
        self.started_at = 0.0
        if self.paused:
            self._resume_state = "dispatched"
            self.state = "paused"
        return {**item.message(), "paused": self.paused}

    def _dispatch_next(self, now: float) -> list[dict[str, Any]]:
        if self.paused:
            return [self.status_message()]
        self._expire_queue(now)
        messages: list[dict[str, Any]] = []
        if self.queue:
            messages.append(self._dispatch(self.queue.pop(0), now))
        messages.append(self.status_message())
        return messages

    def _is_stale(self, item: SpeechItem, now: float) -> bool:
        if self.item_valid is not None and not self.item_valid(item):
            return True
        return self.turn_is_stale(item.turn_id, item.created_at, item.expiry_extension)

    def _expire_queue(self, now: float) -> int:
        if self.paused:
            return 0
        before = len(self.queue)
        kept = []
        for item in self.queue:
            if self._is_stale(item, now):
                if self.on_state:
                    self.on_state(item, 'failed', '排队语音已过时')
            else:
                kept.append(item)
        self.queue = kept
        expired = before - len(self.queue)
        if expired:
            self.dropped += expired
            self.last_reason = f"丢弃{expired}条过时语音"
        return expired

    def _trim_queue(self) -> None:
        limit = max(0, self.cfg.max_queue)
        while len(self.queue) > limit:
            victim = min(self.queue, key=lambda item: (item.priority, item.created_at))
            self.queue.remove(victim)
            if self.on_state:
                self.on_state(victim, 'failed', '播放队列已满')
            self._drop(f"队列已满，丢弃低优先级语音#{victim.id}")

    def _drop(self, reason: str) -> None:
        self.dropped += 1
        self.last_reason = reason
