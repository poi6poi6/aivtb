"""Short-term conversational continuity for live interactions."""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass

from .events import Event
from .memory_facts import record_owner
from .topic_flow import TopicFrame, TopicTracker
from .viewer_identity import viewer_identity

_LIGHT_CHAT = re.compile(r"^(你好|嗨|哈喽|来了|在吗|666|哈哈+)[呀啊嘛]?[！!。.]?$", re.I)
_LATER_STEP = re.compile(
    r"(?:待会儿?|一会儿|等下|之后|随后|回头|接下来|下一步|然后)"
    r"[，,\s]*(?:(?:我(?:们)?|咱们)(?:会|要|来)?|再|就|一起|接着|帮你|给你)*"
    r"(?P<step>(?:挑|选|看|理顺|整理|检查|写|改|安排|讨论|讲|解释|搭配|比较|梳理)"
    r"[^，,。.!！?？;；\n]{1,50})"
)
_NON_PLAN = re.compile(
    r"如果|假如|假设|要是|倘若|以前|曾经|当时|那时|后来|原本|原先|之前|昨天|去年|上次|"
    r"说过|他说|她说|你说|不(?:会|打算|准备)|不用|无需|不必|不要|别"
)
_FINISHED = re.compile(r"(?:已经|都)?(?:选好|挑好|定好|做完|完成|解决|弄好|处理好|搭好)了?|就这么(?:穿|定|办|做)|就这样(?:定了|办|吧)?")
_NOT_FINISHED = re.compile(r"没|未|还不|不算|不代表|不等于|不是|不要|别|假如|如果|要是|[?？]")
_CHANGE_TOPIC = re.compile(
    r"^(?:嗯|好|好的|行)?[，,\s]*(?:换(?:一?个)?话题|聊点别的|先不聊这个(?:话题)?|这个先不聊)"
    r"(?:吧|了)?(?:[，,。.!！\s]+|$)")
_CANCEL_STEP = re.compile(
    r"取消|(?:先)?不(?:用|想|要|必|再)?(?:再|继续)?(?:帮我|给我)?(?:看|挑|选|讲|讨论|安排|继续|做)|"
    r"(?:看|挑|选|讲|讨论|安排|做)[^，,。.!！?？;；]{0,8}(?:算了|先不|不用)|"
    r"(?:不用|不要|不想要)(?:了)?[。.!！\s]*$")
_NOT_CANCEL = re.compile(
    r"(?:不(?:用|要|想|会|能|打算)?|没(?:有|打算)?|别).{0,3}取消|"
    r"不是|别忘|不要忘|不想忘|如果|假如|要是|[?？]")
_GENERIC_FINISH = re.compile(r"^(?:(?:嗯|好|好的|行|那)[，,\s]*)*(?:就这么(?:穿|定|办|做)|就这样(?:定了|办|吧)?|都(?:做好|弄好|处理好|完成)了)[。.!！\s]*$")
_CONTINUE = re.compile(r"^(?:嗯|好|行)?[，,\s]*(?:继续(?:刚才的)?|你继续|接着说|然后呢|下一步呢|接下来呢|回到刚才的话题)[。!?？！，\s]*$")
_NOT_ADVANCED = re.compile(
    r"待会|一会儿|等下|之后|随后|回头|接下来|下一步|然后|稍后|以后|还没|还未|尚未|"
    r"没(?:有)?(?:挑|选|看|做)|不(?:用|要|会|能)|[?？“”「」『』\"']")
_ACTUAL_STEP = re.compile(r"选|挑|用|搭|配|改成|改为|适合|保留|换成|写成|第一|因为|具体|例如|比如")
_TOPIC_NOISE = {
    "先挑", "先选", "之后", "然后", "待会", "等下", "回头", "接下", "下来", "下一", "一步",
    "我们", "帮你", "给你", "一起", "再看", "再选", "再挑", "一下", "一点", "这个", "那个",
    "可以", "事情", "安排", "讨论", "整理", "检查", "解释", "梳理",
}


def _progress_terms(text: str) -> frozenset[str]:
    """Small lexical anchors; unrelated chat must not summon old arrangements."""
    terms = set()
    for part in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]{3,}", text[:200]):
        if part.isascii():
            terms.add(part.lower())
        else:
            terms.update(part[index:index + 2] for index in range(len(part) - 1))
    return frozenset(terms - _TOPIC_NOISE)


def _planned_steps(reply: str):
    for sentence in re.findall(r"[^。.!！?？;；\n]+[。.!！?？;；]?", reply):
        if _NON_PLAN.search(sentence) or re.search(r'[?？“”「」『』"\']|[吗呢][。.!！]?$' , sentence):
            continue
        for match in _LATER_STEP.finditer(sentence):
            step = match['step'].strip()
            if _progress_terms(step):
                yield sentence.strip()[:160], step


def _delivered_step(reply: str, terms: frozenset[str]) -> str:
    for sentence in re.findall(r"[^。.!！?？;；\n]+[。.!！?？;；]?", reply):
        # A trailing caution does not undo concrete advice already delivered:
        # “外套选短一点，别把腰线压住。” has advanced the coat discussion.
        claim = re.split(r"[，,](?=(?:别|不要|不用|不必))", sentence, maxsplit=1)[0]
        if _NON_PLAN.search(claim) or _NOT_ADVANCED.search(claim):
            continue
        if terms & _progress_terms(claim) and _ACTUAL_STEP.search(claim):
            return sentence.strip()[:160]
    return ''


def is_backchannel(text: str) -> bool:
    """Only complete, unambiguous acknowledgements; never swallow a correction."""
    clean = re.sub(r"[\s，。！？,.!?、～~]+", "", text).lower()
    return clean in {"嗯", "嗯嗯", "嗯哼", "对", "对对", "是的", "没错", "好", "好的", "哦", "噢", "然后呢", "继续", "你继续", "接着说", "我在听"}


def memory_change_owner(memory: dict) -> str | None:
    """None denotes a global memory; unknown personal owners stay quarantined."""
    source = memory.get('original_source') or memory.get('source', {})
    if (memory.get('owner_key') or source.get('user')
            or str(memory.get('subject', '')).startswith('观众')):
        # viewer_identity only emits local/account/unkeyed keys (or an empty
        # string). Missing provenance must not match anonymous/system events.
        return record_owner(memory) or 'unknown-memory:' + str(memory.get('slot', ''))
    return None


class VoiceRevisionWindow:
    """Defer durable voice facts while the latest transcript can still be corrected."""

    def __init__(self) -> None:
        self.latest: Event | None = None
        self.pending: list[tuple[Event, str, str]] = []

    def accepts(self, event: Event) -> bool:
        return event.corrects is None or (
            self.latest is not None and self.latest.ui_id == event.corrects
            and viewer_identity(self.latest) == viewer_identity(event) and event.source == "voice"
        )

    def advance(self, event: Event) -> list[tuple[Event, str, str]]:
        if not self.accepts(event):
            raise ValueError("Only the latest voice transcript may be corrected")
        committed = [] if event.corrects is not None else list(self.pending)
        self.pending.clear()
        self.latest = event if event.source == "voice" and not event.retract else None
        return committed

    def stage(self, event: Event, text: str, emotion: str) -> bool:
        if self.latest is not None and self.latest.ui_id == event.ui_id:
            self.pending.append((event, text, emotion))
            return True
        return False

    def apply_memory_change(self, memory: dict) -> int | None:
        owner = memory_change_owner(memory)
        self.pending = [(event, text, emotion) for event, text, emotion in self.pending
                        if owner is not None and viewer_identity(event) != owner]
        if self.latest is not None and (owner is None or viewer_identity(self.latest) == owner):
            closed_id = self.latest.ui_id
            self.latest = None
            return closed_id
        return None


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str
    user: str | None
    ts: float
    event_kind: str = "chat"
    turn_id: int | None = None
    owner_key: str = ""


@dataclass
class ConversationProgress:
    owner_key: str
    turn_id: int | None
    event_ts: float
    text: str
    next_step: str
    terms: frozenset[str]
    next_terms: frozenset[str]
    state: str = "pending"
    advanced_turn_id: int | None = None
    advanced_text: str = ""
    closed_turn_id: int | None = None
    closed_at: float | None = None
    topic_id: str = ""


class ConversationState:
    def __init__(self, max_turns: int = 16) -> None:
        self.turns: deque[ConversationTurn] = deque(maxlen=max(6, max_turns))
        self.topics = TopicTracker()
        self._topic_watermarks: dict[str, tuple[float, int | None]] = {}
        self.current_topic = ""
        self.active_speaker: str | None = None
        self.pending_question = ""
        self.pending_question_at = 0.0
        self.unresolved_promises: deque[str] = deque(maxlen=4)
        self._progress: deque[ConversationProgress] = deque(maxlen=32)
        self._progress_focus: dict[str, ConversationProgress] = {}
        self._progress_closures: deque[tuple[str, float, int | None, str]] = deque(maxlen=64)
        self._reply_receipts: deque[tuple] = deque(maxlen=128)
        self.last_interruption = ""
        self.interrupted_topic: tuple[int | None, str, str | None] | None = None
        self._interrupted_topic_id = ""
        self.retracted_ids: deque[int] = deque(maxlen=32)
        self.memory_cutoffs: dict[str | None, float] = {}
        self.memory_updates: dict[str, tuple[str | None, str]] = {}

    def apply_memory_change(self, memory: dict, replacement: str | None, cutoff: float) -> None:
        owner = memory_change_owner(memory)
        self.memory_cutoffs[owner] = cutoff
        self.topics.forget_owner(owner, cutoff=cutoff)
        self._topic_watermarks = {
            key: stamp for key, stamp in self._topic_watermarks.items()
            if (owner is not None and key != owner) or stamp[0] > cutoff
        }
        # Clear this owner's old conversational context, including derived assistant claims.
        self.turns = deque((turn for turn in self.turns if (owner is not None and turn.owner_key != owner) or turn.ts > cutoff),
                           maxlen=self.turns.maxlen)
        self.current_topic = ''
        self.pending_question = ''
        self._progress = deque((item for item in self._progress
                                if owner is not None and item.owner_key != owner), maxlen=32)
        self._progress_closures = deque((item for item in self._progress_closures
                                        if owner is not None and item[0] != owner), maxlen=64)
        self._reply_receipts = deque((item for item in self._reply_receipts
                                     if owner is not None and item[0] != owner), maxlen=128)
        self._sync_progress()
        self.last_interruption = ''
        self.interrupted_topic = None
        self._interrupted_topic_id = ""
        note = ('用户已在记忆管理中修正，管理时确认的内容：' + replacement
                if replacement is not None else '用户已在记忆管理中要求忘记一条事实；不要从旧对话补回、猜测或复述它。')
        self.memory_updates[memory['slot']] = (owner, note)
        if len(self.memory_updates) > 16:
            del self.memory_updates[next(iter(self.memory_updates))]

    def _obsolete(self, event: Event) -> bool:
        return event.ts <= max(self.memory_cutoffs.get(viewer_identity(event), float('-inf')),
                               self.memory_cutoffs.get(None, float('-inf')))

    def observe_event(self, event: Event) -> None:
        if self._obsolete(event) or getattr(event, "ui_id", None) in self.retracted_ids:
            return
        if event.kind not in {"chat", "gift", "death", "find"}:
            return
        if event.kind == "chat":
            frame = self.topics.observe(event)
            owner = viewer_identity(event)
            if owner and self._is_current_topic_turn(event):
                self._topic_watermarks.pop(owner, None)
                self._topic_watermarks[owner] = (event.ts, getattr(event, "ui_id", None))
                if len(self._topic_watermarks) > 128:
                    del self._topic_watermarks[next(iter(self._topic_watermarks))]
                self._observe_progress(event, frame)
                ended_topic_id = frame.ended_topic_id or (frame.topic_id if frame.action == "close" else "")
                if (ended_topic_id and self.interrupted_topic
                        and self.interrupted_topic[2] == owner
                        and (not self._interrupted_topic_id
                             or self._interrupted_topic_id == ended_topic_id)):
                    self.interrupted_topic = None
                    self._interrupted_topic_id = ""
                    self.last_interruption = ""
        self.turns.append(
            ConversationTurn("audience", event.text[:240], event.user, event.ts, event.kind,
                             getattr(event, "ui_id", None), viewer_identity(event))
        )
        if event.user:
            self.active_speaker = event.user
        clean = event.text.strip()
        if len(clean) >= 4 and not _LIGHT_CHAT.match(clean):
            self.current_topic = clean[:80]
        if self.pending_question and event.ts >= self.pending_question_at:
            self.pending_question = ""

    def record_reply(self, event: Event, reply: str) -> None:
        if self._obsolete(event) or getattr(event, 'ui_id', None) in self.retracted_ids:
            return
        receipt = (viewer_identity(event), getattr(event, 'ui_id', None), event.ts, reply)
        if receipt in self._reply_receipts:
            return
        self._reply_receipts.append(receipt)
        self.topics.record_reply(event, reply)
        now = time.time()
        self.turns.append(
            ConversationTurn("assistant", reply[:400], event.user, now, event.kind,
                             getattr(event, "ui_id", None), viewer_identity(event))
        )
        if self._is_current_topic_turn(event) and ("？" in reply or "?" in reply):
            self.pending_question = reply[-160:]
            self.pending_question_at = now
        self._record_progress(event, reply)

    def _is_current_topic_turn(self, event: Event) -> bool:
        stamp = self._topic_watermarks.get(viewer_identity(event))
        return (stamp is None or event.ts > stamp[0]
                or (event.ts == stamp[0] and getattr(event, "ui_id", None) == stamp[1]))

    def _topic_ended_for(self, event: Event, frame: TopicFrame) -> bool:
        return any(target == viewer_identity(event) and event.ts <= cutoff
                   and (not topic_id or topic_id == frame.topic_id)
                   for target, cutoff, _, topic_id in self._progress_closures)

    def _sync_progress(self) -> None:
        active = [item for item in self._progress if item.state in {'pending', 'advanced'}]
        self.unresolved_promises.clear()
        self.unresolved_promises.extend(item.text for item in active[-4:])
        self._progress_focus = {owner: item for owner, item in self._progress_focus.items()
                                if any(item is current for current in active)}

    def _observe_progress(self, event: Event, frame: TopicFrame) -> None:
        owner = viewer_identity(event)
        if not owner or getattr(event, 'ui_id', None) in self.retracted_ids:
            return
        text = event.text.strip()
        terms = _progress_terms(text)
        active = [item for item in self._progress if item.owner_key == owner
                  and item.state in {'pending', 'advanced'} and item.event_ts <= event.ts]
        related = [item for item in active if terms & item.terms]
        focus = self._progress_focus.get(owner)
        if frame.action == 'resume':
            resumed = [item for item in active if item.topic_id == frame.topic_id]
            if resumed:
                focus = resumed[-1]
                self._progress_focus[owner] = focus
        changed_topic = frame.action != 'close' and bool(frame.ended_topic_id or _CHANGE_TOPIC.match(text))
        cancelled = bool(_CANCEL_STEP.search(text) and not _NOT_CANCEL.search(text))
        finished = bool(_FINISHED.search(text) and not _NOT_FINISHED.search(text))
        targets = []
        if frame.action == 'close':
            topic_items = [item for item in active if item.topic_id == frame.topic_id]
            # The topic classifier may deliberately keep an ambiguous remark
            # attached to its old anchor. Closing that remark must not silently
            # complete an arrangement the dialogue has already moved away from.
            if (focus in topic_items or any(item in related for item in topic_items)
                    or _CHANGE_TOPIC.match(text)):
                targets = topic_items
            if not targets and focus in active and not focus.topic_id:
                targets = [focus]
            finished = frame.reason == 'resolved'
            cancelled = not finished
        elif changed_topic:
            previous = frame.ended_topic_id or frame.previous_topic_id or frame.topic_id
            targets = [item for item in active if not item.topic_id or item.topic_id == previous]
        elif frame.action in {'shift', 'aside'}:
            # An implicit new subject or a brief aside does not complete the
            # arrangement left behind. It stays available for an actual resume.
            self._progress_focus.pop(owner, None)
        elif cancelled:
            targets = related or ([focus] if focus in active else [])
        elif finished:
            targets = [item for item in active if terms & item.next_terms]
            if not targets and _GENERIC_FINISH.fullmatch(text) and focus in active:
                targets = [focus]
        if targets or changed_topic or frame.action == 'close':
            # A delayed segment from the previous turn cannot reopen a closed plan.
            closed_topics = {item.topic_id for item in targets}
            if frame.ended_topic_id or frame.action == 'close':
                closed_topics.add(frame.ended_topic_id or frame.topic_id)
            for topic_id in closed_topics:
                self._progress_closures.append((owner, event.ts, getattr(event, 'ui_id', None), topic_id))
            for item in targets:
                item.state = 'cancelled' if changed_topic or cancelled else 'completed'
                item.closed_turn_id = getattr(event, 'ui_id', None)
                item.closed_at = event.ts
        elif related:
            if frame.action not in {'shift', 'aside'}:
                self._progress_focus[owner] = related[-1]
        elif (frame.action != 'resume' and frame.reason != 'acknowledgement'
              and not is_backchannel(text) and not _CONTINUE.fullmatch(text)):
            self._progress_focus.pop(owner, None)
        self._sync_progress()

    def _record_progress(self, event: Event, reply: str) -> None:
        owner = viewer_identity(event)
        if not owner or event.kind != 'chat' or not self._is_current_topic_turn(event):
            return
        frame = self.topics.inspect(event)
        if frame.action == 'close':
            return
        if self._topic_ended_for(event, frame):
            return
        turn_id = getattr(event, 'ui_id', None)
        for item in self._progress:
            delivered_step = _delivered_step(reply, item.next_terms)
            if (item.owner_key == owner and item.state in {'pending', 'advanced'}
                    and (not item.topic_id or item.topic_id == frame.topic_id)
                    and event.ts > item.event_ts and delivered_step
                    and (_progress_terms(event.text) & item.terms
                         or self._progress_focus.get(owner) is item)):
                item.state = 'advanced'
                item.advanced_turn_id = turn_id
                item.advanced_text = delivered_step
                self._progress_focus[owner] = item
        for text, step in _planned_steps(reply):
            if any(item.owner_key == owner and item.turn_id == turn_id
                   and item.event_ts == event.ts and item.next_step == step for item in self._progress):
                continue
            delivered = ''.join(receipt[3] for receipt in self._reply_receipts
                                if receipt[:3] == (owner, turn_id, event.ts))
            item = ConversationProgress(owner, turn_id, event.ts, text, step,
                                        _progress_terms(delivered), _progress_terms(step),
                                        topic_id=frame.topic_id)
            owned = [old for old in self._progress if old.owner_key == owner
                     and old.state in {'pending', 'advanced'}]
            if len(owned) >= 4:
                self._progress.remove(owned[0])
            self._progress.append(item)
            self._progress_focus[owner] = item
        self._sync_progress()

    def mark_interrupted(self, event: Event) -> None:
        if self._obsolete(event) or not self._is_current_topic_turn(event):
            return
        if getattr(event, "ui_id", None) in self.retracted_ids:
            return
        frame = self.topics.inspect(event)
        if frame.action == 'close' or self._topic_ended_for(event, frame):
            return
        if not is_backchannel(event.text):
            self.interrupted_topic = (getattr(event, "ui_id", None), event.text[:240], viewer_identity(event))
            self._interrupted_topic_id = (frame.topic_id
                                          if viewer_identity(event) in self._topic_watermarks else "")
        if not self.last_interruption:
            self.last_interruption = f"刚才回应“{event.text[:60]}”时被打断；未播完的内容不能视为对方已听到"

    def retract_turn(self, turn_id: int) -> None:
        self.retracted_ids.append(turn_id)
        self.topics.retract_turn(turn_id)
        if self.interrupted_topic and self.interrupted_topic[0] == turn_id:
            self.interrupted_topic = None
            self._interrupted_topic_id = ""
        self.turns = deque((turn for turn in self.turns if turn.turn_id != turn_id), maxlen=self.turns.maxlen)
        for owner, stamp in list(self._topic_watermarks.items()):
            if stamp[1] == turn_id:
                latest = next((turn for turn in reversed(self.turns)
                               if turn.role == 'audience' and turn.owner_key == owner), None)
                if latest:
                    self._topic_watermarks[owner] = (latest.ts, latest.turn_id)
                else:
                    del self._topic_watermarks[owner]
        self.pending_question = ""
        self.current_topic = next((turn.text[:80] for turn in reversed(self.turns)
                                   if turn.role == "audience" and len(turn.text) >= 4), "")
        self._progress = deque((item for item in self._progress if item.turn_id != turn_id), maxlen=32)
        self._progress_closures = deque((item for item in self._progress_closures if item[2] != turn_id),
                                        maxlen=64)
        self._reply_receipts = deque((item for item in self._reply_receipts if item[1] != turn_id),
                                     maxlen=128)
        for item in self._progress:
            if item.advanced_turn_id == turn_id:
                item.advanced_turn_id, item.advanced_text = None, ''
                if item.state == 'advanced':
                    item.state = 'pending'
            if item.closed_turn_id == turn_id:
                item.closed_turn_id, item.closed_at = None, None
                item.state = 'advanced' if item.advanced_text else 'pending'
                self._progress_focus[item.owner_key] = item
        self._sync_progress()
        self.last_interruption = "上一条语音识别有误，旧转写及基于它的回答已撤出对话上下文；请按当前修正后的内容重新理解，不要复述错误版本"

    def record_partial_playback(self, text: str, seconds: float) -> None:
        self.last_interruption = (
            f"上一段语音只播放了约{seconds:.1f}秒便被打断。该段原计划说：{text[:180]}。"
            "没有逐字对齐，无法确认具体听到了哪些词；不要把整段当成已说完，也不要假设对方听到了后续内容"
        )

    def record_playback(self, item, completed: bool) -> Event | None:
        event = Event(item.event.get("source", "voice"), item.event.get("kind", "chat"),
                      item.event.get("text", ""), user=item.event.get("user"),
                      ts=item.event.get("ts", item.created_at),
                      viewer_key=item.event.get('viewer_key', ''),
                      related_messages=[dict(message) for message in item.event.get('related_messages', [])])
        if self._obsolete(event):
            return None
        event.ui_id = item.turn_id
        if item.turn_id in self.retracted_ids:
            return None
        if not completed:
            frame = self.topics.inspect(event)
            if (not self._is_current_topic_turn(event) or frame.action == 'close'
                    or self._topic_ended_for(event, frame)):
                return None
            self.interrupted_topic = (item.turn_id, item.event['text'][:240], viewer_identity(event))
            self._interrupted_topic_id = (frame.topic_id
                                          if viewer_identity(event) in self._topic_watermarks else "")
            self.record_partial_playback(item.text, item.played_seconds)
            return None
        self.record_reply(event, item.text)
        return event

    def correction_context_for(self, event: Event) -> str:
        """Read scoped memory corrections without advancing conversation state."""
        owner = viewer_identity(event)
        return '\n'.join(note for target, note in self.memory_updates.values()
                         if target is None or target == owner)

    def context_for(self, event: Event) -> str:
        lines: list[str] = []
        owner = viewer_identity(event)
        frame = self.topics.inspect(event)
        corrections = self.correction_context_for(event)
        if corrections:
            lines.append(corrections)
        same_interrupted_topic = (
            not self._interrupted_topic_id or frame.topic_id == self._interrupted_topic_id
            or (frame.action == 'aside' and frame.previous_topic_id == self._interrupted_topic_id)
        )
        if (self.interrupted_topic and self.interrupted_topic[2] == owner
                and frame.action != 'close' and same_interrupted_topic):
            turn_id, topic, _ = self.interrupted_topic
            delivered = [turn.text for turn in self.turns
                         if turn.turn_id == turn_id and turn.role == 'assistant']
            lines.append(f"被打断、尚未讲完的话题：{topic}")
            lines.append("该话题已完整说出的段落：" + (" / ".join(delivered) or "暂无完整段落"))
            if re.sub(r"[\s，。！？,.!?]+", "", event.text) in {"继续", "你继续", "接着说", "继续刚才的", "回到刚才的话题"}:
                lines.append("用户希望继续：承接上述话题，跳过已完整说出的段落；未播完段落可简短衔接，不要从头重讲。")
                self.interrupted_topic = None
                self._interrupted_topic_id = ""
            else:
                lines.append("先回答本次插问；仅在话题相关时简短询问是否接着讲，别自动追加整段旧回答。")
        topic_context = self.topics.context(frame)
        if topic_context:
            lines.append(topic_context)
        if self.active_speaker:
            lines.append(f"近期主要互动观众：{self.active_speaker}")
        terms = _progress_terms(event.text)
        continuing = frame.action == 'resume' or bool(_CONTINUE.fullmatch(event.text.strip()))
        for item in self._progress:
            if item.owner_key != owner:
                continue
            same_topic = not item.topic_id or item.topic_id == frame.topic_id
            relevant = same_topic and (bool(terms & item.terms)
                                      or (continuing and self._progress_focus.get(owner) is item))
            if item.state in {'pending', 'advanced'} and relevant:
                detail = ("已经谈到：" + item.advanced_text + "；等待对方确认，不重复从头安排。"
                          if item.state == 'advanced' else "下一步：" + item.next_step)
                lines.append("这段共同对话的进展：" + item.text + "；" + detail)
            elif item.closed_at == event.ts and item.closed_turn_id == getattr(event, 'ui_id', None):
                lines.append("对方已" + ("完成" if item.state == 'completed' else "取消或结束")
                             + "这段共同安排；简短收尾，不再重复旧待办：" + item.next_step)
        if self.last_interruption and (self.interrupted_topic is None or same_interrupted_topic):
            lines.append(self.last_interruption + "；先回应对方新补充或纠正，不要机械重播旧回答。")
            self.last_interruption = ""
        recent = [turn for turn in self.turns if turn.ts <= event.ts][-8:]
        if recent:
            rendered = []
            for turn in recent:
                who = "助手" if turn.role == "assistant" else (turn.user or "观众")
                rendered.append(f"{who}：{turn.text}")
            lines.append("最近对话（公开历史；已结束的话题不因此恢复，也不自动续聊）：\n"
                         + "\n".join(rendered))
        return "\n".join(lines)
