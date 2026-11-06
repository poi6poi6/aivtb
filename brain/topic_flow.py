"""Account-scoped topic continuity with conservative, reversible local rules.

Input and delivered-reply receipts form a small session log. Replaying that log
keeps previews read-only and makes transcript corrections retractable. Topic
text remains untrusted context data; guidance is selected only from constants.
"""
from __future__ import annotations

import hashlib
import copy
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .events import Event
from .viewer_identity import viewer_identity


_BREAK = re.compile(r"[，,。.!！?？;；\n]")
_QUOTE = re.compile(r"""“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*"|'[^']*'""")
_REPORT = re.compile(r"^\s*(?:他|她|他们|她们|别人|朋友|同事|老板|对方)"
                     r"(?:刚才|之前|一直|总是|曾经|又|也)?(?:说|问|提到|让我|叫我)")
_NEGATED = re.compile(r"(?:不是|并非|不要|不用|别|没有|还没|没).{0,6}$")
_SWITCH = re.compile(r"(?:换(?:一?个)?话题|聊点别的|先不聊这个(?:话题)?|这个先不聊)"
                     r"(?:吧|了|呀|嘛)?(?:好吗|可以吗|行吗|好不好)?")
_END = re.compile(
    r"(?:^|[，,])\s*(?:那|好|好的|嗯|行|好了)?\s*(?:今天)?\s*"
    r"(?:不用聊了|不用再(?:聊(?:这个(?:话题)?)?|安慰(?:我)?)"
    r"|(?:先)?不聊这个(?:话题)?了|(?:这件事|这个话题|这个问题)?(?:先)?不聊了"
    r"|(?:这件事|这个话题|这个问题)?(?:就)?到此为止"
    r"|到这[里儿]吧|(?:先)?聊到这[里儿](?:吧|了)?"
    r"|先这样吧|(?:先)?就这样(?:吧|了)?|这个话题(?:先)?结束|这个问题不用再说了"
    r"|我没事了|我(?:现在|已经)?好多了|(?:已经|现在)?好多了)"
    r"(?:吧|了|呀|嘛)?(?:好吗|可以吗|行吗|好不好)?\s*(?=$|[，,])")
_RESOLVED = re.compile(
    r"(?:^|[，,])\s*(?:(?:这件事|事情|这个问题|问题)(?:已经)?解决了"
    r"|都(?:弄清楚|处理好|做好|完成)了|我知道该怎么办了|就这么(?:穿|定|办|做))\s*(?=$|[，,])")
_ASIDE = re.compile(r"^\s*(?:顺便问(?:一下|一句)?|插一句|插个问题|岔开一下)[，,:：\s]*")
_RESUME = re.compile(r"^\s*(?:嗯|好|行)?[，,\s]*(?:继续刚才(?:的|那个)?(?:话题|问题)?"
                     r"|回到刚才(?:的|那个)?(?:话题|问题)?|接着聊刚才(?:的|那个)?)[，,:：\s]*")
_OPEN = re.compile(r"^\s*(?:(?:对了|另外|说起来)[，,\s]*"
                   r"|(?:我们|我想|陪我)?(?:聊聊|聊一下|谈谈|说说))")
_REFERENCE = re.compile(r"^\s*(?:主要是|其实|后来|然后|那|这样|这个|那个|它|他|她|他们|她们"
                        r"|补充(?:一下|一点)?|还有一点|我是说|不是[，, ]*(?:我说的是|我是说)"
                        r"|不是这个意思|你刚才|你说的|你说得)")
_CONTINUE = re.compile(r"^(?:继续|你继续|接着说|然后呢|接下来呢|下一步呢|我在听)[。!?？！，,\s]*$")
_POLITE_REMAINDER = re.compile(r"^(?:(?:吧|了|呀|嘛|好吗|可以吗|行吗|好不好)(?:[，,:：\s]+|$))+")
_QUESTION = re.compile(r"怎么|如何|为什么|什么|多少|多大|多久|哪(?:个|种|里)|能不能|可以吗|会不会|[?？]"
                       r"|帮我|给我|请(?:讲|说|解释|推荐)")
_INDEPENDENT_STATEMENT = re.compile(
    r"^(?:(?:我|我们)(?:的)?)?(?:今天|昨天|明天|刚才|刚刚|最近|这几天).{4,}")
_DOMAINS = (
    ("plants", re.compile(r"玫瑰|兰花|绣球|浇水|施肥|盆栽|花盆|植物|种花|养花")),
    ("clothes", re.compile(r"外套|裙子|黑裙|衬衫|穿搭|围巾|鞋子|领带|衣服|搭配")),
    ("computing", re.compile(r"电脑|显卡|内存|CPU|Python|程序|代码|报错|麦克风", re.I)),
    ("cooking", re.compile(r"做饭|菜谱|炒菜|煮面|烤箱|食材|午饭|晚饭|咖啡(?!色)")),
    ("travel", re.compile(r"旅行|机票|酒店|火车票|旅游|航班")),
    ("study_work", re.compile(r"面试|简历|考试|作业|论文|工作|开会|同事")),
    ("personal_concern", re.compile(r"我.{0,6}(?:难过|焦虑|失恋|委屈|害怕)|我失恋|我被欺负")),
    ("arts", re.compile(r"电影|电视剧|番剧|动漫|音乐|歌曲")),
)
_NOISE = {"这个", "那个", "怎么", "什么", "如何", "可以", "能不", "不能", "会不", "不会",
          "觉得", "你觉", "我觉", "一下", "一点", "后来", "然后", "其实", "主要", "要是",
          "还是", "的是", "的话", "帮我", "给我", "我们", "你说", "说的", "说得", "继续",
          "话题", "问题", "对了", "另外", "说说", "聊聊", "刚才"}


@dataclass(frozen=True)
class TopicFrame:
    action: str = ""
    topic_id: str = ""
    anchor: str = ""
    focus: str = ""
    delivered: tuple[str, ...] = ()
    pending_question: str = ""
    previous_topic_id: str = ""
    reason: str = ""
    ended_topic_id: str = ""


@dataclass(frozen=True)
class _Input:
    token: tuple
    turn_id: int | None
    at: float
    event: Event


@dataclass(frozen=True)
class _Delivery:
    token: tuple
    at: float
    text: str
    topic_id: str


@dataclass
class _Topic:
    id: str
    anchor: str
    focus: str
    delivered: list[str] = field(default_factory=list)
    question: str = ""
    status: str = "active"
    aside: bool = False


@dataclass
class _World:
    topics: dict[str, _Topic] = field(default_factory=dict)
    current: str = ""
    main: str = ""
    suspended: list[str] = field(default_factory=list)
    frames: dict[tuple, TopicFrame] = field(default_factory=dict)


def _direct(text: str) -> str:
    unquoted = _QUOTE.sub("", text[:4000])
    return "，".join(clause for clause in _BREAK.split(unquoted) if not _REPORT.search(clause)).strip("， ")


def _active(pattern: re.Pattern, text: str):
    for match in pattern.finditer(text):
        prefix = _BREAK.split(text[:match.start()])[-1]
        if not _NEGATED.search(prefix):
            yield match


def _ack(text: str) -> bool:
    clean = re.sub(r"[\s，,。.!！?？、～~]+", "", text)
    return clean in {"嗯", "嗯嗯", "哦", "噢", "好", "好的", "行", "谢谢", "谢谢你", "好的谢谢",
                     "明白了", "知道了", "了解了", "原来如此", "是的", "对", "对的", "没错",
                     "你好", "嗨", "哈哈", "这样啊"}


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[A-Za-z0-9]{3,}", text.lower()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(part[i:i + 2] for i in range(len(part) - 1))
    return terms - _NOISE


def _domains(text: str) -> set[str]:
    return {name for name, pattern in _DOMAINS if pattern.search(text)}


class TopicTracker:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        idle_timeout_sec: float = 1200, max_owners: int = 128, max_events: int = 32,
    ):
        self._clock, self._wall_clock = clock, wall_clock
        self.idle_timeout_sec = max(1., float(idle_timeout_sec))
        self.max_owners, self.max_events = max(1, int(max_owners)), max(2, int(max_events))
        self._owners: OrderedDict[str, list[_Input | _Delivery]] = OrderedDict()
        self._checkpoints: dict[str, _World] = {}
        self._seen: OrderedDict[tuple, Event | None] = OrderedDict()
        self._reply_seen: OrderedDict[tuple, None] = OrderedDict()
        self._retracted: OrderedDict[int, None] = OrderedDict()
        self._barriers: OrderedDict[str, float] = OrderedDict()
        self._all_cutoff = -math.inf
        self._limit = max(64, self.max_owners * self.max_events * 2)

    @staticmethod
    def _turn_id(event: Event) -> int | None:
        value = getattr(event, "ui_id", None)
        return value if type(value) is int and value > 0 else None

    def _token(self, event: Event) -> tuple:
        turn_id = self._turn_id(event)
        return (viewer_identity(event), "turn", turn_id) if turn_id else (
            viewer_identity(event), "object", id(event))

    def _eligible(self, event: Event) -> bool:
        if (event.kind != "chat" or event.source not in {"voice", "companion", "danmaku", "game", "qq_group"}
                or not viewer_identity(event) or event.agent_reply or event.task_id
                or event.task_notice or event.retract or self._turn_id(event) in self._retracted):
            return False
        cutoff = max(self._all_cutoff, self._barriers.get(viewer_identity(event), -math.inf))
        return cutoff == -math.inf or (
            isinstance(event.ts, (float, int)) and math.isfinite(event.ts) and event.ts > cutoff)

    def _history(self, owner: str) -> list[_Input | _Delivery]:
        history = self._owners.get(owner, [])
        return history if history and self._clock() - history[-1].at < self.idle_timeout_sec else []

    def _input(self, event: Event) -> _Input:
        snapshot = replace(event, text=str(event.text or "")[:4000], related_messages=[])
        return _Input(self._token(event), self._turn_id(event), self._clock(), snapshot)

    @staticmethod
    def _snapshot(topic: _Topic | None, action: str = "", *, previous: str = "",
                  reason: str = "", ended: str = "", focus: str = "") -> TopicFrame:
        return TopicFrame(
            action=action, topic_id=topic.id if topic else "", anchor=topic.anchor if topic else "",
            focus=focus or (topic.focus if topic else ""),
            delivered=tuple(topic.delivered) if topic else (),
            pending_question=topic.question if topic else "",
            previous_topic_id=previous, reason=reason, ended_topic_id=ended,
        )

    @staticmethod
    def _suspend(world: _World, topic: _Topic | None) -> None:
        if topic is None or topic.status == "closed":
            return
        topic.status = "suspended"
        world.suspended = [key for key in world.suspended if key != topic.id] + [topic.id]
        world.suspended = world.suspended[-4:]

    @staticmethod
    def _close(world: _World, topic: _Topic | None) -> None:
        if topic is None:
            return
        topic.status = "closed"
        topic.question = ""
        world.suspended = [key for key in world.suspended if key != topic.id]
        if world.current == topic.id:
            world.current = ""
        if world.main == topic.id:
            world.main = ""

    def _start(self, world: _World, item: _Input, text: str, *, aside: bool = False) -> _Topic:
        topic_id = "topic-" + hashlib.sha256(repr(item.token).encode()).hexdigest()[:16]
        topic = _Topic(topic_id, text[:240], text[:240], aside=aside)
        world.topics[topic_id] = topic
        world.current = topic_id
        if not aside:
            world.main = topic_id
        return topic

    def _advance(self, world: _World, item: _Input) -> TopicFrame:
        raw = item.event.text.strip()
        text = _direct(raw)
        current = world.topics.get(world.current)
        if current is not None and current.status == "closed":
            current = None
        previous = current.id if current else ""
        switches = list(_active(_SWITCH, text))
        ends = list(_active(_END, text))
        resolved = list(_active(_RESOLVED, text))
        closures = [(match.end(), match, kind) for matches, kind in
                    ((switches, "explicit_shift"), (ends, "explicit_end"), (resolved, "resolved"))
                    for match in matches]
        if closures:
            _, match, reason = max(closures, key=lambda entry: entry[0])
            self._close(world, current)
            remainder = text[match.end():].strip("，,:： ")
            remainder = _POLITE_REMAINDER.sub("", remainder).strip("，,:： ")
            if remainder and not _ack(remainder) and not _CONTINUE.fullmatch(remainder):
                topic = self._start(world, item, remainder)
                return self._snapshot(topic, "shift" if previous else "start",
                                      previous=previous, ended=previous, reason=reason)
            # A bare request to change subject closes the old focus without
            # inventing the next subject or declaring an unfinished task done.
            return self._snapshot(current, "close" if previous else "",
                                  previous=previous, ended=previous, reason=reason, focus=raw[:240])

        resumed = _RESUME.match(text)
        if resumed:
            candidates = []
            if current and current.aside and world.main:
                candidates.append(world.main)
            candidates.extend(reversed(world.suspended))
            target = next((world.topics[key] for key in candidates if key in world.topics
                           and world.topics[key].status != "closed"), None)
            if target is None:
                target = current
            if target is None:
                return self._snapshot(None, "uncertain", reason="no_open_topic", focus=raw[:240])
            if target is not current:
                self._suspend(world, current)
            target.status = "active"
            world.current = target.id
            world.main = target.id if not target.aside else world.main
            world.suspended = [key for key in world.suspended if key != target.id]
            remainder = text[resumed.end():].strip("，,:： ")
            if remainder:
                target.focus = remainder[:240]
            return self._snapshot(target, "resume", previous=previous, reason="explicit_return")

        aside = _ASIDE.match(text)
        if aside and current:
            self._suspend(world, current)
            topic = self._start(world, item, raw, aside=True)
            return self._snapshot(topic, "aside", previous=previous, reason="explicit_aside")
        if not text or _ack(text):
            return self._snapshot(current, "continue" if current else "", reason="acknowledgement")
        if current is None:
            # Naming a concrete entity can return to one suspended topic after
            # another topic ended. A broad domain or generic overlap alone is
            # insufficient, and explicitly closed topics are never candidates.
            named = []
            for key in world.suspended:
                target = world.topics.get(key)
                if target is None or target.status != "suspended":
                    continue
                evidence = " ".join([target.anchor, target.focus, *target.delivered])
                if _domains(text) & _domains(evidence) and _terms(text) & _terms(evidence):
                    named.append(target)
            if len(named) == 1:
                target = named[0]
                target.status = "active"
                target.focus, target.question = raw[:240], ""
                world.current = target.id
                if not target.aside:
                    world.main = target.id
                world.suspended = [key for key in world.suspended if key != target.id]
                return self._snapshot(target, "resume", reason="named_return")
            if _CONTINUE.fullmatch(text) or _REFERENCE.match(text):
                return self._snapshot(None, "uncertain", reason="no_open_topic", focus=raw[:240])
            topic = self._start(world, item, raw)
            return self._snapshot(topic, "start", reason="first_content")
        if _CONTINUE.fullmatch(text):
            return self._snapshot(current, "continue", reason="continuation")
        if _REFERENCE.match(text):
            current.focus, current.question = raw[:240], ""
            return self._snapshot(current, "continue", reason="supplement")

        evidence = " ".join([current.anchor, current.focus, *current.delivered])
        old_domains, new_domains = _domains(evidence), _domains(text)
        related = bool(_terms(text) & _terms(evidence) or old_domains & new_domains)
        explicit_open = bool(_OPEN.match(text))
        new_domain = bool(old_domains and new_domains and not old_domains & new_domains
                          and (_QUESTION.search(raw) or _INDEPENDENT_STATEMENT.match(text)))
        if (explicit_open and not related) or new_domain:
            self._suspend(world, current)
            topic = self._start(world, item, raw)
            return self._snapshot(topic, "shift", previous=previous,
                                  reason="new_domain" if new_domain else "explicit_start")
        current.focus, current.question = raw[:240], ""
        if not related and _QUESTION.search(raw):
            return self._snapshot(current, "uncertain", reason="ambiguous_connection")
        return self._snapshot(current, "continue", reason="related" if related else "supplement")

    def _replay(self, history: list[_Input | _Delivery], *, owner: str = "") -> _World:
        checkpoint = self._checkpoints.get(owner) if history else None
        world = copy.deepcopy(checkpoint) if checkpoint else _World()
        for entry in history:
            if isinstance(entry, _Input):
                world.frames[entry.token] = self._advance(world, entry)
                continue
            frame = world.frames.get(entry.token)
            topic = world.topics.get(frame.topic_id if frame else entry.topic_id)
            if topic is None or topic.status == "closed":
                continue
            topic.delivered.append(entry.text)
            topic.delivered = topic.delivered[-3:]
            questions = re.findall(r"[^。.!！?？]+[?？]", entry.text)
            if questions:
                topic.question = questions[-1].strip()[:240]
        return world

    def inspect(self, event: Event) -> TopicFrame:
        if not self._eligible(event):
            return TopicFrame()
        token = self._token(event)
        history = self._history(token[0])
        world = self._replay(history, owner=token[0])
        if token in world.frames:
            return world.frames[token]
        if token in self._seen:
            return TopicFrame()
        return self._advance(world, self._input(event))

    def observe(self, event: Event) -> TopicFrame:
        if not self._eligible(event):
            return TopicFrame()
        token = self._token(event)
        if token in self._seen:
            return self.inspect(event)
        now = self._clock()
        for owner, history in list(self._owners.items()):
            if now - history[-1].at >= self.idle_timeout_sec:
                self._owners.pop(owner)
                self._checkpoints.pop(owner, None)
        history = self._owners.pop(token[0], [])
        history.append(self._input(event))
        indices = [index for index, entry in enumerate(history) if isinstance(entry, _Input)]
        if len(indices) > self.max_events:
            split = indices[-self.max_events]
            checkpoint = self._replay(history[:split], owner=token[0])
            # Retain only the bounded active/suspended topic state. Input frames
            # stay in the correction window; topic identity survives its slide.
            retained = {checkpoint.current, checkpoint.main, *checkpoint.suspended}
            checkpoint.topics = {key: value for key, value in checkpoint.topics.items()
                                 if key in retained and value.status != "closed"}
            checkpoint.frames.clear()
            self._checkpoints[token[0]] = checkpoint
            history = history[split:]
        self._owners[token[0]] = history
        while len(self._owners) > self.max_owners:
            owner, _ = self._owners.popitem(last=False)
            self._checkpoints.pop(owner, None)
        self._seen[token] = event if self._turn_id(event) is None else None
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
        return self._replay(history, owner=token[0]).frames[token]

    def record_reply(self, event: Event, text: str) -> bool:
        """Attach only an actually delivered reply; never open a topic here."""
        if not self._eligible(event) or not isinstance(text, str) or not text.strip():
            return False
        token = self._token(event)
        text = text.strip()[:400]
        receipt = (token, text)
        if receipt in self._reply_seen:
            return False
        history = self._history(token[0])
        world = self._replay(history, owner=token[0])
        frame = world.frames.get(token)
        topic = world.topics.get(frame.topic_id) if frame else None
        if topic is None or topic.status == "closed":
            return False
        history.append(_Delivery(token, self._clock(), text, topic.id))
        # Bound segmented receipts independently from the number of inputs.
        deliveries = [entry for entry in history if isinstance(entry, _Delivery) and entry.token == token]
        if len(deliveries) > 6:
            history.remove(deliveries[0])
        self._reply_seen[receipt] = None
        while len(self._reply_seen) > self._limit * 3:
            self._reply_seen.popitem(last=False)
        return True

    def retract_turn(self, turn_id: int) -> None:
        if type(turn_id) is not int or turn_id <= 0 or turn_id in self._retracted:
            return
        self._retracted[turn_id] = None
        while len(self._retracted) > self._limit:
            self._retracted.popitem(last=False)
        for owner, history in list(self._owners.items()):
            removed = {entry.token for entry in history
                       if isinstance(entry, _Input) and entry.turn_id == turn_id}
            if not removed and (owner, "turn", turn_id) in self._seen:
                # The correction predates the replay window. Its exact effects
                # cannot be undone safely, so discard only this owner's state.
                self._owners.pop(owner)
                self._checkpoints.pop(owner, None)
                continue
            kept = [entry for entry in history if entry.token not in removed]
            if kept:
                self._owners[owner] = kept
            else:
                self._owners.pop(owner)
                self._checkpoints.pop(owner, None)

    def forget_owner(self, owner: str | None = None, *, cutoff: float | None = None) -> None:
        cutoff = self._wall_clock() if cutoff is None else float(cutoff)
        if not math.isfinite(cutoff):
            raise ValueError("topic cutoff must be a finite event timestamp")
        if owner is None:
            self._owners.clear()
            self._checkpoints.clear()
            self._all_cutoff = max(self._all_cutoff, cutoff)
            self._barriers.clear()
        else:
            self._owners.pop(owner, None)
            self._checkpoints.pop(owner, None)
            self._barriers[owner] = max(self._barriers.get(owner, -math.inf), cutoff)
            self._barriers.move_to_end(owner)
            while len(self._barriers) > self._limit:
                self._barriers.popitem(last=False)

    @staticmethod
    def context(frame: TopicFrame) -> str:
        if not frame.action and not frame.topic_id:
            return ""
        return json.dumps({
            "topic_id": frame.topic_id, "action": frame.action, "reason": frame.reason,
            "anchor": frame.anchor, "focus": frame.focus, "delivered": list(frame.delivered),
            "pending_question": frame.pending_question, "previous_topic_id": frame.previous_topic_id,
            "ended_topic_id": frame.ended_topic_id,
        }, ensure_ascii=False)

    @staticmethod
    def guidance(frame: TopicFrame) -> str:
        return {
            "start": "这轮开始了一个话题。直接回应新内容，不生硬宣布话题编号，也不硬接无关的旧事。",
            "continue": "继续当前话题，结合本轮补充推进一点；普通应答不是结束信号，已经完整说过的内容不要从头重复。",
            "shift": "本轮转到新的话题焦点，直接回应新内容。没有明确结束的旧题只是暂放，不声称已经解决或完成。",
            "aside": "本轮是临时插问，先回应插问；主话题仍保留，不把两件事混在一起，也不自动长篇拉回主话题。",
            "resume": "对方明确回到尚未结束的话题。承接此前已经说完的内容和最新重点，不从头复述背景。",
            "close": "对方明确结束了当前话题，简短自然收尾即可，不追问、不再补讲旧题；结束聊天不等于现实任务已完成。",
            "uncertain": "当前内容与旧题的关系尚不确定，先回应眼前这句话；不硬拉旧题，也不擅自宣布旧题结束。只有影响理解时才简短确认。",
        }.get(frame.action, "")
