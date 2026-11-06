"""Bounded, account-scoped conversational stance, using only local rules.

This is session state, not a profile or a diagnosis. Only explicit requests and
clear distress establish a stance. Prompt guidance is always chosen from fixed
constants; user text is never promoted into instructions.
"""
from __future__ import annotations

import re
import time
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from . import safety
from .events import Event
from .viewer_identity import viewer_identity

if TYPE_CHECKING:
    from .topic_flow import TopicFrame, TopicTracker


_BREAK = re.compile(r"[，,。.!！?？;；\n]")
_NEGATED = re.compile(r"(?:不是|并非|不用|无需|不要|不必|不想|没有|还没|没|别).{0,6}$")
_QUOTED = re.compile(r"""“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*"|'[^']*'""")
_REPORTED = re.compile(
    r"^\s*(?:他|她|他们|她们|别人|朋友|同事|老板|对方)"
    r"(?:刚才|之前|刚刚|一直|总是|曾经|又|也)?(?:说|问|提到|让我|叫我)"
)
_MODE_PATTERNS = (
    ("listening", re.compile(
        r"先听我说|听我说完|只想(?:倾诉|说说|让你听)|(?:别|不要|先别|不用)(?:给我|给|提)?建议"
        r"|先别(?:分析|帮我分析)|你先听着")),
    ("reflection", re.compile(
        r"(?:帮我|陪我|和我一起)(?:分析|梳理|理清)|一起(?:分析|梳理)|先梳理一下")),
    ("advice", re.compile(
        r"(?:给我|给)(?:一点|一些|两|三|几|一|[123])?(?:个|条|点|些)?(?:具体的?)?(?:建议|办法|方案)"
        r"|(?:现在)?有什么(?:建议|办法)|我(?:现在)?该怎么办|帮我(?:想个|想想|找个)(?:办法|方案)")),
    ("playful", re.compile(r"逗逗?我|逗我笑|给我讲个笑话|讲个笑话吧|陪我轻松聊|轻松一点聊")),
)
_REPAIR_PATTERNS = (
    ("fact", re.compile(r"你(?:说|记|弄)错了|你说[得的]不对|纠正一下|更正一下|事实不是这样|这个说法不准确")),
    ("need", re.compile(r"你(?:没|没有)理解我的意思|你(?:刚才)?(?:理解|听)错了|你误会(?:我|了)|不是这个意思|我不是(?:让你|要你|想要)"
                        r"|你(?:没|没有)听懂|我需要的不是")),
    ("style", re.compile(r"别说教|不要说教|别复读|不要复读|别挖苦|不要挖苦|别阴阳怪气|别嘲讽"
                         r"|(?:刚才|你说[得的]|回答)?太长了|你说[得的]太快|太敷衍了")),
    ("perspective", re.compile(r"我不这么看|我们(?:的)?看法不同|我的看法不同|这点我不同意"
                               r"|我不同意(?:你|这个看法|这个观点|[。！!，,]|$)")),
)
_CLOSE = re.compile(
    r"换(?:个|一个)?话题|聊点别的|不聊这个了|不用聊了|先不聊了|不用再聊|不用再安慰"
    r"|(?:^|[，,])\s*(?:那|今天)?(?:先)?就这样(?:吧|了)?\s*(?=$|[，,])"
    r"|先这样吧|我没事了|我(?:已经|现在)?好多了"
    r"|(?:^|[，,])\s*(?:已经|现在)?好多了\s*(?=$|[，,])"
    r"|(?:这件事|事情|问题)(?:已经)?解决了"
)
_TECHNICAL = re.compile(r"压力测试|(?:程序|软件|系统|游戏|进程)崩溃(?:日志)?")
_DISTRESS = re.compile(
    r"(?:我|最近|今天|真的|有点|特别|很|好|太).{0,14}(?:难过|难受|焦虑|害怕|失眠|崩溃|委屈|孤独|压力|失恋|被欺负)"
    r"|(?:亲人|家人|母亲|父亲|妈妈|爸爸).{0,8}(?:去世|过世)"
    r"|我.{0,8}(?:被家暴|被霸凌|遭到暴力)|只想倾诉|认真听我"
)
_DENIED_DISTRESS = re.compile(
    r"(?:不再|并没有|没有|并不|并非|不是|不会|不|没)"
    r"(?:(?:真的|特别|那么|这么|太|很|怎么|感到|觉得|有|什么)){0,3}"
    r"(?:难过|难受|焦虑|害怕|失眠|崩溃|委屈|孤独|压力|失恋|想死|自杀|伤害自己)"
)


@dataclass(frozen=True)
class DialogueFrame:
    mode: str = ""        # listening | reflection | advice | playful
    repair: str = ""      # need | fact | style | perspective
    care: bool = False
    closed: bool = False
    topic_changed: bool = False


@dataclass(frozen=True)
class _Observation:
    token: tuple
    turn_id: int | None
    at: float
    signal: DialogueFrame
    # Re-evaluate topic membership when a corrected input changes the timeline.
    event_ref: Event | None = None


def _active(pattern: re.Pattern, text: str):
    for match in pattern.finditer(text):
        prefix = _BREAK.split(text[:match.start()])[-1]
        if not _NEGATED.search(prefix):
            yield match


class DialoguePolicy:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic,
        idle_timeout_sec: float = 900, max_turns: int = 12,
        max_owners: int = 128, repair_turns: int = 3,
        care_detector: Callable[[Event], bool] | None = None,
        wall_clock: Callable[[], float] = time.time,
        topics: TopicTracker | None = None,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self.idle_timeout_sec = max(1., float(idle_timeout_sec))
        self.max_turns = max(1, int(max_turns))
        self.max_owners = max(1, int(max_owners))
        self.repair_turns = max(1, int(repair_turns))
        self._care_detector = care_detector
        self.topics = topics
        self._owners: OrderedDict[str, list[_Observation]] = OrderedDict()
        self._seen: OrderedDict[tuple, Event | None] = OrderedDict()
        self._retracted: OrderedDict[int, None] = OrderedDict()
        self._seen_limit = max(64, self.max_owners * self.max_turns * 2)
        self._barriers: OrderedDict[str, float] = OrderedDict()
        self._all_cutoff = -math.inf

    @staticmethod
    def _eligible(event: Event) -> bool:
        return bool(
            event.kind == "chat" and event.source in {"voice", "companion", "danmaku", "game", "qq_group"}
            and viewer_identity(event) and not event.agent_reply and not event.task_notice
            and not event.task_id and not event.retract
        )

    @staticmethod
    def _turn_id(event: Event) -> int | None:
        turn_id = getattr(event, "ui_id", None)
        return turn_id if type(turn_id) is int and turn_id > 0 else None

    def _token(self, event: Event) -> tuple:
        turn_id = self._turn_id(event)
        return (viewer_identity(event), "turn", turn_id) if turn_id else (
            viewer_identity(event), "object", id(event))

    def _signal(self, event: Event) -> DialogueFrame:
        unquoted = _QUOTED.sub("", str(event.text or "")[:4000])
        text = "，".join(clause for clause in _BREAK.split(unquoted)
                        if not _REPORTED.search(clause))
        closes = list(_active(_CLOSE, text))
        closed_at = closes[-1].start() if closes else -1
        mode, repair = "", ""
        for patterns, field in ((_MODE_PATTERNS, "mode"), (_REPAIR_PATTERNS, "repair")):
            matches = [(match.start(), value) for value, pattern in patterns
                       for match in _active(pattern, text) if match.start() > closed_at]
            value = max(matches, default=(-1, ""))[1]
            if field == "mode":
                mode = value
            else:
                repair = value
        # Closure settles the old concern; a fresh direct request may still
        # establish the mode of the new topic in the same message.
        care_text = text[closes[-1].end():] if closes else text
        care_text = _DENIED_DISTRESS.sub("", care_text)
        care_event = replace(event, text=care_text)
        care = (bool(self._care_detector(care_event)) if self._care_detector else
                safety.is_crisis(care_text) or bool(_DISTRESS.search(_TECHNICAL.sub("", care_text))))
        return DialogueFrame(mode, repair, care, bool(closes))

    def _blocked(self, event: Event) -> bool:
        if self._turn_id(event) in self._retracted:
            return True
        cutoff = max(self._all_cutoff, self._barriers.get(viewer_identity(event), -math.inf))
        if cutoff == -math.inf:
            return False
        return (not isinstance(event.ts, (float, int)) or not math.isfinite(event.ts)
                or event.ts <= cutoff)

    def _history(self, owner: str) -> list[_Observation]:
        history = self._owners.get(owner, [])
        if history and self._clock() - history[-1].at < self.idle_timeout_sec:
            return history
        return []

    def _frame(self, history: list[_Observation], topic: TopicFrame | None = None) -> DialogueFrame:
        if topic is not None:
            # Re-evaluate topic membership after a transcript retraction. An
            # aside has its own stance; returning to the main topic restores
            # its earlier requests without importing the aside's mood.
            history = [item for item in history
                       if item.event_ref is not None
                       and self.topics.inspect(item.event_ref).topic_id == topic.topic_id]
        mode, repair, care, repair_left = "", "", False, 0
        for observation in history:
            signal = observation.signal
            if signal.closed:
                mode, repair, care, repair_left = "", "", False, 0
            if signal.mode:
                mode = signal.mode
            elif signal.care and mode == "playful":
                mode = ""
            care = care or signal.care
            if signal.repair:
                repair, repair_left = signal.repair, self.repair_turns
            elif repair_left:
                repair_left -= 1
                if not repair_left:
                    repair = ""
        closed = topic.action == "close" if topic is not None else bool(history and history[-1].signal.closed)
        changed = topic is not None and topic.action in {"start", "shift", "aside", "resume"}
        return DialogueFrame(mode, repair, care, closed, changed)

    def _topic(self, event: Event):
        return self.topics.inspect(event) if self.topics is not None else None

    def _topic_signal(self, event: Event, topic: TopicFrame | None) -> DialogueFrame:
        signal = self._signal(event)
        # The shared lifecycle is authoritative about whether a topic ended.
        return replace(signal, closed=topic.action == "close") if topic is not None else signal

    def inspect(self, event: Event) -> DialogueFrame:
        """Preview this turn without changing history, timers, or deduplication."""
        if not self._eligible(event) or self._blocked(event):
            return DialogueFrame()
        token = self._token(event)
        history = self._history(token[0])
        topic = self._topic(event)
        for index, observation in enumerate(history):
            if observation.token == token:
                return self._frame(history[:index + 1], topic)
        if token in self._seen:
            return DialogueFrame()
        preview = _Observation(token, self._turn_id(event), self._clock(),
                               self._topic_signal(event, topic), event)
        return self._frame((history + [preview])[-self.max_turns:], topic)

    def observe(self, event: Event) -> DialogueFrame:
        if not self._eligible(event) or self._blocked(event):
            return DialogueFrame()
        token = self._token(event)
        if token in self._seen:
            return self.inspect(event)
        now = self._clock()
        for owner, history in list(self._owners.items()):
            if now - history[-1].at >= self.idle_timeout_sec:
                self._owners.pop(owner)
        history = self._owners.pop(token[0], [])
        topic = self._topic(event)
        history.append(_Observation(token, self._turn_id(event), now, self._topic_signal(event, topic), event))
        self._owners[token[0]] = history[-self.max_turns:]
        while len(self._owners) > self.max_owners:
            self._owners.popitem(last=False)
        self._seen[token] = event if self._turn_id(event) is None else None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
        return self._frame(self._owners[token[0]], topic)

    def retract_turn(self, turn_id: int) -> None:
        """Remove verified input; replay retained successors without its stance."""
        if type(turn_id) is not int or turn_id <= 0:
            return
        self._retracted[turn_id] = None
        while len(self._retracted) > self._seen_limit:
            self._retracted.popitem(last=False)
        for owner, history in list(self._owners.items()):
            kept = [observation for observation in history if observation.turn_id != turn_id]
            if kept:
                self._owners[owner] = kept
            else:
                self._owners.pop(owner)

    def forget_owner(self, owner: str | None = None, *, cutoff: float | None = None) -> None:
        # Keep bounded seen tokens so late duplicate events cannot recreate the
        # state that was deliberately cleared.
        cutoff = self._wall_clock() if cutoff is None else float(cutoff)
        if not math.isfinite(cutoff):
            raise ValueError("dialogue cutoff must be a finite event timestamp")
        if owner is None:
            self._owners.clear()
            self._all_cutoff = max(self._all_cutoff, cutoff)
            self._barriers.clear()
        else:
            self._owners.pop(owner, None)
            self._barriers[owner] = max(self._barriers.get(owner, -math.inf), cutoff)
            self._barriers.move_to_end(owner)
            while len(self._barriers) > self._seen_limit:
                self._barriers.popitem(last=False)

    @staticmethod
    def context(frame: DialogueFrame) -> str:
        lines = []
        if frame.closed:
            lines.append("对方已明确收尾或换话题：简短接住即可，不续讲旧问题、不追加追问；若同条消息提出新内容，直接回应新内容。")
        if frame.care:
            lines.append("当前同一人的认真困扰尚未明确结束；轻声、具体地承接已知内容，不因本轮没有情绪词就突然调侃，也不推测未表达的情绪或诊断。")
        mode = {
            "listening": "当前需要先被倾听：接住已经说出的具体内容，留出继续说的空间；暂不主动给方案，不机械追问。",
            "reflection": "当前明确希望一起梳理：厘清已知事实、感受与未确定之处；不要急着替对方下结论或开处方。",
            "advice": "当前明确希望获得办法：围绕已知情况给少量具体建议；无需重复询问是否想听建议，也不让长期倾听偏好覆盖本次请求。",
            "playful": "当前明确希望轻松一点：可以温和幽默，保留角色自身品味；不拿尚未结束的困扰开玩笑，不强迫对方开心。",
        }.get(frame.mode)
        if mode:
            lines.append(mode)
        repair = {
            "need": "对方指出需求被误解：仅在尚未回应这次纠正时简短承认理解偏差，随后直接体现需求调整；后续轮次不重复承认或道歉，不辩解、不重复原来的答法。",
            "fact": "对方对事实提出更正：核对已有依据，确认错误再修正，尚不能确认就说明不确定；不盲从纠正，也不把分歧当作关系恶化。",
            "style": "对方明确指出表达方式不合适：仅在尚未回应这次反馈时简短接住，后续用实际表达体现调整，避免说教、复读或反复道歉；不因此惩罚关系亲疏。",
            "perspective": "双方观点或品味不同：允许保留各自判断，说明必要依据和分歧，不争输赢、不假装同意；有新事实再修正。",
        }.get(frame.repair)
        if repair:
            lines.append(repair)
        return ("本轮互动分寸（仅调整沟通方式，服从当前事实与角色、安全边界）：\n" +
                "\n".join(lines)) if lines else ""
