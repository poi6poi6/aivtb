"""自主直播导演：根据参与度、反馈和话题状态管理节目节奏。"""
from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable

from .config import DirectorConfig
from .events import PRIO_IDLE, Event
from .viewer_identity import viewer_identity

PHASE_LABELS = {
    "opening": "开场",
    "chatting": "闲聊",
    "interaction": "全场互动",
    "highlight": "高能时刻",
    "closing": "收尾",
}
PHASES = frozenset(PHASE_LABELS)

_LIGHT_CHAT = re.compile(r"^(你好|晚上好|早上好|嗨|哈喽|来了|在吗|666|哈哈哈*[呀啊]?)\W*$", re.I)
_HIGH_ENERGY_KINDS = frozenset({"gift", "death", "find"})
_GENERIC_IDLE_TEXT = frozenset({"", "直播间冷场了"})
_EMOTION_ENERGY = {
    "开心": 0.72, "得意": 0.78, "惊讶": 0.90,
    "吐槽": 0.66, "委屈": 0.48, "平静": 0.35,
}

# These rules intentionally recognise clear answers only. Unknown wording keeps the
# invitation open instead of claiming that an unrelated message answered it.
_QUESTION = re.compile(r"[?？]|什么|哪|怎么|多少|是否|还是|有没有|要不要|[吗呢][呀啊]?\s*$")
_DEVICE_FEEDBACK = re.compile(
    r"(?:声音|音量|麦克风|麦|音乐|伴奏).{0,8}(?:太小|太大|小了|大了|听不清|听不到|没开|关了|爆音|杂音)"
    r"|没(?:有)?声音|听不(?:到|见|清)|听不到|(?:声音|音频|画面|直播|视频).{0,6}(?:卡|断|糊|延迟|不同步|没反应)"
    r"|(?:音画|字幕).{0,6}不同步|(?:有|听见).{0,3}回声|(?:卡住|卡了|黑屏|掉线|断线)"
)
_ANSWER_DOMAINS = (
    (r"什么花|哪.{0,3}花|花卉|(?:画|种|养|喜欢).{0,10}花", r"玫瑰|月季|百合|蔷薇|郁金香|向日葵|绣球|茉莉|雏菊|菊花|兰花|樱花|莲花|荷花|牡丹|芍药|桔梗|风信子|满天星|薰衣草|山茶|睡莲|水仙|鸢尾|勿忘我"),
    (r"颜色|配色|什么色", r"红色|橙色|黄色|绿色|青色|蓝色|紫色|粉色|黑色|白色|灰色|棕色|银色|金色|薄荷绿|墨绿|天蓝|奶白|米白|莫兰迪|^[红橙黄绿青蓝紫粉黑白灰棕]$"),
    (r"心情|过得|感觉怎么样", r"开心|高兴|难过|伤心|累|疲惫|烦|焦虑|紧张|平静|放松|不错|还好|一般|糟|无聊|忙|失落|轻松|快乐"),
    (r"吃什么|吃点|食物|甜点|饭|菜", r"饭|菜|面|粥|饺|包子|馄饨|蛋糕|布丁|冰淇淋|火锅|烧烤|牛排|寿司|披萨|汉堡|水果|饼|巧克力"),
    (r"喝什么|喝点|茶还是|咖啡还是|饮料", r"茶|咖啡|牛奶|果汁|可乐|汽水|水|巧克力"),
)


def _reply_question(reply: str) -> str:
    """Remember an actual invitation, not the director's intended question."""
    clean = re.sub(r"\[[^\]]{1,16}\]", "", reply).strip()
    sentences = re.findall(r"[^。！!\n？?]+[？?]?", clean)
    return next((text.strip()[-300:] for text in reversed(sentences) if _QUESTION.search(text)), "")


def _answer_matches(question: str, reply: str) -> bool:
    text = reply.strip(" ，。！？!?~～")
    if not text or _LIGHT_CHAT.match(text) or _QUESTION.search(text):
        return False
    # A/B or numeric selections are accepted only when the question offers them.
    if "还是" in question or "或者" in question or "二选一" in question:
        labels = re.findall(r"(?<![a-zA-Z0-9])[A-D1-4](?![a-zA-Z0-9])", question, re.I)
        if any(re.fullmatch(r"(?:我|就|我就)?(?:选|选第|要|选项)?\s*" + re.escape(label)
                            + r"(?:个|项)?(?:吧|呀|啊)?", text, re.I) for label in labels):
            return True
        choices = re.split(r"还是|或者", question)
        for choice in choices:
            choice = re.split(r"[，,：:]", choice)[-1]
            choice = re.sub(r"^(?:(?:你们|大家|你|我们|更|比较|想|喜欢|希望|要|画|种|选择|选|是|喝|吃|点)\s*)+", "", choice)
            choice = choice.strip(" 呢吗呀啊吧？?。").lower()
            if len(choice) >= 2 and choice in text.lower():
                return True
    for question_pattern, answer_pattern in _ANSWER_DOMAINS:
        if re.search(question_pattern, question) and re.search(answer_pattern, text):
            return True
    if re.search(r"(?:想|要|希望|喜欢).{0,3}(?:聊|分享).{0,4}(?:什么|哪)|今天.{0,5}(?:分享|有趣的事)", question):
        # An open topic invitation accepts a substantive suggestion, but not a
        # greeting, another question, or a device report (filtered by the caller).
        return len(text) >= 2
    # Keep meaningful topic overlap as a conservative fallback for other topics.
    stripped = re.sub(r"大家|你们|我们|觉得|喜欢|想要|什么|哪个|哪些|怎么|为什么|时候|今天|现在|一下|一个|可以|有没有|有没有人|有人", " ", question.lower())
    words = re.findall(r"[a-z][a-z0-9_-]{2,}", stripped)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}", stripped)
    meaningful = {token[i:i + 2] for token in tokens for i in range(len(token) - 1)
                  if not re.search(r"[的了呢吗呀啊是有你我他她这那很更都要能会]", token[i:i + 2])}
    return (any(token in text.lower() for token in meaningful)
            or any(re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text, re.I) for word in words))


class LiveDirector:
    """事件驱动的节目状态机；只控制节奏，不替代角色人格。"""

    def __init__(self, cfg: DirectorConfig, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._clock = clock
        self.started_at = clock()
        self.last_reply_at = self.started_at
        self.phase = "opening" if cfg.enabled else "chatting"
        self.manual_phase: str | None = None
        self.phase_turns = 0
        self.total_turns = 0
        self.turns_since_interaction = 0
        self.current_topic = ""
        self.topic_turns = 0
        self.recent_topics: deque[str] = deque(maxlen=5)
        self.mood = "平静"
        self.energy = 0.30

        self.awaiting_audience = False
        self.question_pending = False
        self.audience_question_id: int | None = None
        self.question_pending_at = 0.0
        self.audience_question_at = 0.0
        self.audience_question = ""
        self.audience_poll_id = ""
        self.audience_poll_deadline = 0.0
        self._participating_poll_id = ""
        self._successful_poll_ids: deque[str] = deque(maxlen=64)
        self.question_responses = 0
        self.successful_interactions = 0
        self.missed_interactions = 0

        self._return_phase = "chatting"
        self._viewer_seen: dict[str, float] = {}
        self._viewer_names: dict[str, str] = {}
        self._events: deque[tuple[float, str | None, str]] = deque()
        self._idle_index = 0
        self._topic_seed_index = 0
        self._topic_fatigued = False
        self._opening_prompt_sent = False
        self._last_proactive_at = 0.0
        self._program_context: dict = {}
        self._program_spoken: set[tuple] = set()
        self._topic_seen: dict[str, float] = {}
        self._conversation_topic_owner = ""
        self._conversation_topic_id = ""
        self._conversation_topic_paused = False
        self._conversation_topic_generation = 0
        self.topic_provider = None

    def _formal_topic_active(self) -> bool:
        return bool(self._program_context.get("live_mode") or
                    (self.audience_poll_id and self.audience_poll_deadline > self._clock()))

    @staticmethod
    def _ordinary_owner_chat(event: Event) -> bool:
        return (event.source in {"companion", "voice"} and event.kind == "chat"
                and not event.retract and not event.task_id and not event.task_notice
                and not event.agent_reply and not getattr(event, "host_question_id", None)
                and not getattr(event, "live_poll", None))

    def sync_conversation_topic(self, event: Event, frame) -> None:
        """Apply the shared topic decision; never infer another boundary here."""
        if not self._ordinary_owner_chat(event) or self._formal_topic_active():
            return
        owner = viewer_identity(event)
        if not owner:
            return
        action = getattr(frame, "action", "")
        reason = getattr(frame, "reason", "")
        # A first local message can explicitly end a room/seed topic even
        # though the account-scoped tracker has no private topic to close.
        if not action and reason in {"explicit_end", "explicit_shift", "resolved"}:
            action = "close"
        if action == "close":
            # A different local profile cannot end or reopen this owner's chat.
            if self._conversation_topic_owner not in {"", owner}:
                return
            self._conversation_topic_owner = owner
            self.clear_conversation_topic(owner)
            return
        if action not in {"start", "shift", "resume", "continue"}:
            return
        topic_id = getattr(frame, "topic_id", "")
        topic = str(getattr(frame, "anchor", "") or getattr(frame, "focus", "")).strip()
        if not topic_id or not topic:
            return
        if self._conversation_topic_paused:
            if self._conversation_topic_owner not in {"", owner}:
                return
            text = event.text.strip()
            # A replayed frame after a retraction may say "continue". It can
            # restore the topic only when this new input supplies real content.
            if action == "continue" and (len(text) < 4 or _LIGHT_CHAT.match(text)
                                         or reason in {"acknowledgement", "continuation"}):
                return
        changed = (self._conversation_topic_paused or topic_id != self._conversation_topic_id
                   or owner != self._conversation_topic_owner)
        self._conversation_topic_owner = owner
        self._conversation_topic_id = topic_id
        self._conversation_topic_paused = False
        if changed:
            self._conversation_topic_generation += 1
            self._reset_interaction()
            self.topic_turns = 0
            self.turns_since_interaction = 0
            self._topic_fatigued = False
            if self.manual_phase is None:
                self._enter("chatting")
            self.recent_topics.append(topic[:100])
            self._remember_topic(topic)
        self.current_topic = topic[:100]

    def clear_conversation_topic(self, owner: str | None = None) -> None:
        """Forget a copied private anchor without ending a show or live poll."""
        if owner is not None and owner != self._conversation_topic_owner:
            return
        if owner is None:
            self._conversation_topic_owner = ""
        self._conversation_topic_generation += 1
        self._conversation_topic_id = ""
        self._conversation_topic_paused = True
        self.recent_topics.clear()
        if not self._formal_topic_active():
            self._topic_seen.pop(self._topic_key(self.current_topic), None)
            self.current_topic = ""
            self.topic_turns = 0
            self.turns_since_interaction = 0
            self._topic_fatigued = False
            self._reset_interaction()
            if self.manual_phase is None:
                self._enter("chatting")

    def set_program_context(self, context: dict | None) -> None:
        """Host-selected show facts; enabling this never changes the saved config."""
        context = dict(context or {})
        previous = self._program_context
        if context.get('show_id') != previous.get('show_id'):
            self._program_spoken.clear()
            self._topic_seen.clear()
        if (context.get('show_id'), context.get('card_id'), context.get('stage')) != (
                previous.get('show_id'), previous.get('card_id'), previous.get('stage')):
            self._reset_interaction()
        self._program_context = context
        if context.get('live_mode'):
            self.current_topic = str(context.get('topic', ''))[:100]
            self.phase = 'closing' if context.get('stage') == 'finished' else 'chatting'
        elif previous.get('live_mode'):
            self._reset_interaction()
            self.phase, self.current_topic = 'chatting', ''

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled or bool(self._program_context.get('live_mode'))

    def observe_event(self, event: Event) -> None:
        """所有到达事件都用于判断场面，即使最终没有被仲裁回应。"""
        if not self.enabled:
            return
        now = self._clock()
        self._observe_activity(event)

        waiting = self.awaiting_audience or self.question_pending
        feedback = bool(event.kind == "chat" and _DEVICE_FEEDBACK.search(event.text))
        if (
            self.awaiting_audience
            and not self.audience_poll_id
            and event.source == "danmaku"
            and event.kind == "chat"
            and now >= self.audience_question_at
            and not feedback
            and _answer_matches(self.audience_question, event.text)
        ):
            self.awaiting_audience = False
            self.audience_question_id = None
            self.question_responses += 1
            self.successful_interactions += 1

        # A side conversation may still be answered, but does not replace the
        # unanswered room question or manufacture a successful interaction.
        topic = "" if waiting or feedback or self._conversation_topic_paused else self._topic_from(event)
        if topic and topic != self.current_topic:
            self.current_topic = topic
            self.topic_turns = 0
            self._topic_fatigued = False
            self.recent_topics.append(topic)
            self._remember_topic(topic)

    def _observe_activity(self, event: Event) -> None:
        now = self._clock()
        identity = f"{event.source}:{event.viewer_key or event.user}" if event.viewer_key or event.user else None
        self._events.append((now, identity, event.kind))
        self._prune_events(now)
        if identity:
            self._viewer_seen[identity] = now
            self._viewer_names[identity] = event.user or "观众"
        target = {
            "gift": 0.96, "death": 0.92, "find": 0.82,
            "chat": 0.52, "ambient": 0.42, "idle": 0.24,
        }.get(event.kind, 0.40)
        self.energy = max(self.energy * 0.88, target)

    def observe_poll_vote(self, event: Event, receipt: dict) -> None:
        """Use validated live ballots as participation, without an LLM reply."""
        if (not self.enabled or not receipt.get("ok") or not receipt.get("changed")
                or receipt.get("mode") != "live" or not receipt.get("poll_id")):
            return
        self._observe_activity(event)
        self._participating_poll_id = receipt["poll_id"]
        if self.audience_poll_id == receipt["poll_id"]:
            self._complete_poll_interaction()

    def _complete_poll_interaction(self) -> None:
        poll_id = self.audience_poll_id
        if not poll_id or not (self.awaiting_audience or self.question_pending):
            return
        self.awaiting_audience = False
        self.question_pending = False
        self.audience_question_id = None
        if poll_id not in self._successful_poll_ids:
            self._successful_poll_ids.append(poll_id)
            self.question_responses += 1
            self.successful_interactions += 1

    def prepare(self, event: Event) -> None:
        """选择本轮阶段，并把节目目标附加给人格提示。"""
        if not self.enabled:
            event.director_context = ""
            return
        if self._program_context.get('live_mode'):
            if event.kind == 'idle' and event.text in _GENERIC_IDLE_TEXT:
                event.text = self._program_cue() or '安静陪伴，等观众接话，不催促、不重复提问'
            event.director_context = self._directive(event)
            return
        if self._conversation_topic_paused and not self._formal_topic_active():
            if event.kind == "idle":
                event.text = "安静陪伴，等待用户明确开启或恢复话题"
            event.director_context = self._directive(event)
            return
        self._maybe_auto_close()
        if self.phase == "opening":
            self._opening_prompt_sent = True
        if self.manual_phase is None and event.kind in _HIGH_ENERGY_KINDS:
            if self.phase != "highlight":
                self._return_phase = self.phase if self.phase != "opening" else "chatting"
                self._enter("highlight")
        elif self.manual_phase is None:
            self._maybe_enter_interaction()
        if event.kind == "idle" and event.text in _GENERIC_IDLE_TEXT:
            event.text = self._idle_cue()
        event.director_context = self._directive(event)

    def after_reply(self, event: Event, emotion: str, reply: str) -> None:
        if not self.enabled or not self.program_event_current(event):
            return
        now = self._clock()
        self.last_reply_at = now
        self.total_turns += 1
        self.phase_turns += 1
        self.mood = emotion if emotion in _EMOTION_ENERGY else "平静"
        target = _EMOTION_ENERGY.get(self.mood, 0.40)
        self.energy = max(0.15, min(1.0, self.energy * 0.72 + target * 0.28))

        if self.current_topic and not (self.awaiting_audience or self.question_pending):
            self.topic_turns += 1
            if self.topic_turns >= max(2, self.cfg.topic_max_turns):
                self.current_topic = ""
                self.topic_turns = 0
                self._topic_fatigued = True

        poll = getattr(event, "live_poll", None)
        if poll is not None:
            # Only authoritative live-poll announcements create this association.
            # An independent room question keeps its own answer requirement.
            if poll["mode"] == "live" and poll["state"] == "open" and poll["deadline"] > now:
                if (not (self.awaiting_audience or self.question_pending)
                        or self.audience_poll_id == poll["id"]):
                    if self.manual_phase is None:
                        self._enter("interaction")
                    self._start_waiting_for_audience(event, now, reply)
            elif self.audience_poll_id == poll["id"]:
                self._reset_interaction()
            return

        if self._program_context.get('live_mode'):
            # The host owns the rundown, so chat volume never skips a stage.
            return
        if self._conversation_topic_paused:
            return

        if self.manual_phase is not None:
            if self.phase == "interaction" and not self.awaiting_audience and not self.question_pending:
                self._start_waiting_for_audience(event, now, reply)
            return
        if self.phase == "opening" and self.phase_turns >= max(1, self.cfg.opening_turns):
            if self._should_invite_room():
                self._enter("interaction")
            else:
                self._enter("chatting")
            return
        if self.phase == "highlight" and self.phase_turns >= max(1, self.cfg.highlight_turns):
            self._enter(self._return_phase if self._return_phase in PHASES else "chatting")
            return
        if self.phase == "interaction":
            if self.question_responses > 0:
                self.turns_since_interaction = 0
                self._enter("chatting")
            elif not self.awaiting_audience and not self.question_pending:
                self._start_waiting_for_audience(event, now, reply)
            return
        if self.phase == "chatting":
            self.turns_since_interaction += 1
            self._maybe_enter_interaction()

    def poll_proactive(self, allow: bool = True) -> Event | None:
        """按时间主动安排开场、互动无人回应后的转场或冷场话题。"""
        if not self.enabled or not allow:
            return None
        now = self._clock()
        if self._program_context.get('live_mode'):
            return self._poll_program(now)
        if self._conversation_topic_paused and not self.audience_poll_id:
            return None
        self._maybe_auto_close()
        if (
            self.question_pending
            and now - self.question_pending_at
            >= max(3.0, self.cfg.interaction_audio_start_fallback_sec)
        ):
            # 无浏览器、关闭自动播放或回执丢失时，以文字已经展示作为保守起点。
            self.question_pending = False
            self.awaiting_audience = True
            self.audience_question_at = now
        if (
            self.phase == "opening"
            and not self._conversation_topic_paused
            and self.total_turns == 0
            and not self._opening_prompt_sent
            and now - self.started_at >= max(0.0, self.cfg.opening_delay_sec)
        ):
            self._opening_prompt_sent = True
            return self._proactive_event("自然做一个温暖的简短开场问候，再给观众一个容易回答、没有压力的心情话题", now)

        if (
            self.awaiting_audience
            and (now >= self.audience_poll_deadline if self.audience_poll_id else
                 now - self.audience_question_at >= max(5.0, self.cfg.interaction_response_timeout_sec))
        ):
            self.awaiting_audience = False
            self.audience_question_id = None
            self.audience_question = ""
            self.audience_poll_id = ""
            self.audience_poll_deadline = 0.0
            self.missed_interactions += 1
            if self.manual_phase is None:
                self._enter("chatting")
            if self._conversation_topic_paused:
                return None
            topic = self._next_topic_seed()
            if not topic:
                return None
            return self._proactive_event(
                f"刚才的问题暂时没人接，轻松带过，不要责怪观众；自然转到“{topic}”", now
            )

        if self._conversation_topic_paused:
            return None
        quiet_for = now - self.last_reply_at
        interval = max(8.0, self.cfg.proactive_interval_sec)
        if quiet_for >= interval and now - self._last_proactive_at >= interval * 0.7:
            cue = self._idle_cue()
            return self._proactive_event(cue, now) if cue else None
        return None

    def mark_audio_started(self, event_id: int) -> bool:
        """浏览器确认互动问题开始发声后，才启动观众回应计时。"""
        if not self.question_pending or self.audience_question_id != event_id:
            return False
        self.question_pending = False
        self.awaiting_audience = True
        self.audience_question_at = self._clock()
        self.question_pending_at = 0.0
        return True

    def set_phase(self, phase: str) -> bool:
        """测试台可锁定节目阶段；auto 恢复参与度驱动状态机。"""
        if not self.enabled:
            return False
        if phase == "auto":
            self.manual_phase = None
            target = "opening" if self.total_turns < self.cfg.opening_turns else "chatting"
            self._enter(target)
            return True
        if phase not in PHASES:
            return False
        self.manual_phase = phase
        self._enter(phase)
        if phase == "interaction":
            self._reset_interaction()
        return True

    def summary(self) -> dict:
        now = self._clock()
        self._prune_events(now)
        window = max(30.0, self.cfg.active_viewer_window_sec)
        self._viewer_seen = {
            name: seen for name, seen in self._viewer_seen.items() if now - seen <= window
        }
        self._viewer_names = {key: name for key, name in self._viewer_names.items() if key in self._viewer_seen}
        engagement = self._engagement(now)
        return {
            "enabled": self.enabled,
            "mode": "manual" if self.manual_phase else "auto",
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "topic": self.current_topic,
            "mood": self.mood,
            "energy": round(self.energy, 2),
            "turns": self.total_turns,
            "active_viewers": [self._viewer_names[key] for key in list(self._viewer_seen)[-5:]],
            "active_now": engagement["viewers"],
            "chat_rate": engagement["chat_rate"],
            "engagement": engagement["level"],
            "waiting_for_audience": self.awaiting_audience,
            "question_pending": self.question_pending,
            "audience_question": self.audience_question,
            "audience_poll_id": self.audience_poll_id,
            "successful_interactions": self.successful_interactions,
            "missed_interactions": self.missed_interactions,
        }

    def _enter(self, phase: str) -> None:
        if phase == self.phase:
            return
        previous = self.phase
        self.phase = phase
        self.phase_turns = 0
        if phase == "interaction" and previous != "highlight":
            self._reset_interaction()
        elif phase not in {"interaction", "highlight"}:
            self._reset_interaction()

    def _reset_interaction(self) -> None:
        self.awaiting_audience = False
        self.question_pending = False
        self.audience_question_id = None
        self.question_pending_at = 0.0
        self.audience_question_at = 0.0
        self.audience_question = ""
        self.audience_poll_id = ""
        self.audience_poll_deadline = 0.0
        self.question_responses = 0

    def _start_waiting_for_audience(self, event: Event, now: float, reply: str) -> None:
        poll = getattr(event, "live_poll", None)
        if self._conversation_topic_paused and not poll:
            return
        question = poll["question"] if poll else _reply_question(reply)
        if not question:
            return
        self.audience_question = question
        self.audience_poll_id = poll["id"] if poll else ""
        self.audience_poll_deadline = poll["deadline"] if poll else 0.0
        self.current_topic = question[:42]
        self.topic_turns = 0
        self._topic_fatigued = False
        event_id = getattr(event, "ui_id", None)
        self.audience_question_id = event_id
        if event_id is None:  # 单元测试或无 WebUI 模式：没有播放器回执可等
            self.awaiting_audience = True
            self.question_pending = False
            self.audience_question_at = now
            self.question_pending_at = 0.0
        else:
            self.awaiting_audience = False
            self.question_pending = True
            self.question_pending_at = now
            self.audience_question_at = 0.0
        if poll and (poll["total"] or self._participating_poll_id == poll["id"]):
            self._complete_poll_interaction()

    def _maybe_auto_close(self) -> None:
        minutes = self.cfg.closing_after_minutes
        if self.manual_phase is None and minutes > 0:
            if self._clock() - self.started_at >= minutes * 60:
                self._enter("closing")

    def _maybe_enter_interaction(self) -> None:
        if self.phase != "chatting" or not self._should_invite_room():
            return
        engagement = self._engagement(self._clock())
        base = max(2, self.cfg.interaction_every_turns)
        threshold = max(2, base // 2) if engagement["level"] == "high" else base
        if self.turns_since_interaction >= threshold:
            self._enter("interaction")

    def _should_invite_room(self) -> bool:
        engagement = self._engagement(self._clock())
        return engagement["viewers"] >= max(1, self.cfg.interaction_min_viewers)

    def _prune_events(self, now: float) -> None:
        window = max(30.0, self.cfg.engagement_window_sec)
        while self._events and now - self._events[0][0] > window:
            self._events.popleft()

    def _engagement(self, now: float) -> dict:
        self._prune_events(now)
        window = max(30.0, self.cfg.engagement_window_sec)
        chats = [item for item in self._events if item[2] == "chat"]
        viewers = len({item[1] for item in chats if item[1]})
        chat_rate = round(len(chats) * 60.0 / window, 1)
        high = chat_rate >= max(1.0, self.cfg.high_chat_rate_per_min) and viewers >= 2
        level = "high" if high else "medium" if len(chats) >= 2 else "low"
        return {"viewers": viewers, "chat_rate": chat_rate, "level": level}

    def _topic_from(self, event: Event) -> str:
        if event.source != "danmaku" or event.kind != "chat":
            return ""
        text = re.sub(r"\s+", " ", event.text).strip(" ，。！？!?~～")
        if len(text) < 4 or _LIGHT_CHAT.match(text):
            return ""
        return text[:42]

    def _active_names(self) -> list[str]:
        now = self._clock()
        window = max(30.0, self.cfg.active_viewer_window_sec)
        return [self._viewer_names[key] for key, seen in self._viewer_seen.items() if now - seen <= window][-4:]

    def _next_topic_seed(self) -> str:
        # Consult public trends only where this state machine already permits
        # a new topic. It must never refresh the network or interrupt a topic.
        if callable(self.topic_provider):
            try:
                fresh = self.topic_provider()
            except Exception:
                fresh = ''
            if isinstance(fresh, str) and fresh.strip() and len(fresh) <= 180:
                self._remember_topic(fresh)
                return fresh
        seeds = [str(item).strip() for item in self.cfg.topic_seeds if str(item).strip()]
        if not seeds:
            seeds = ["大家今天最想分享的一件小事"]
        for _ in seeds:
            seed = seeds[self._topic_seed_index % len(seeds)]
            self._topic_seed_index += 1
            key = self._topic_key(seed)
            if self._clock() - self._topic_seen.get(key, -float('inf')) >= 600:
                self._remember_topic(seed)
                return seed
        return ''

    @staticmethod
    def _topic_key(text):
        return re.sub(r'\W+', '', text.casefold())

    def _remember_topic(self, text):
        self._topic_seen[self._topic_key(text)] = self._clock()
        if len(self._topic_seen) > 128:
            self._topic_seen.pop(next(iter(self._topic_seen)))

    def _program_cue(self):
        p = self._program_context
        if not p.get('fresh') or p.get('stage') == 'waiting':
            return ''
        goals = {
            'intro': '简短介绍这轮主题，给观众一个容易跟上的创作方向。',
            'vote': '只依据已确认的投票状态轻松衔接；进行中的投票可提醒按投票画面发送“投票 加编号”，不编造选项或重复催票。',
            'sketch': '简短说明真实准备状态，可以围绕主题分享一个创作想法；不要声称还未完成的草稿已经画好。',
            'discussion': '结合当前已确认状态邀请一个具体的创作建议；没有已准备作品时，不描述尚不存在的画面细节。',
            'naming': '邀请一个贴合本轮主题的名字，主播尚未记录时不能声称命名已确定。',
            'finished': '简短收束这轮主题，留出停顿，不替主播开启下一轮。',
        }
        goal = goals.get(p.get('stage'), '')
        if not goal:
            return ''
        return f"当前栏目“{p.get('topic', '')}”，{p.get('status', '')}。{goal}不要宣布已执行新的投票、绘画、上色或审批操作。"

    def _poll_program(self, now):
        p = self._program_context
        key = (p.get('show_id'), p.get('card_id'), p.get('stage'), p.get('status_code'))
        if key in self._program_spoken or now - self.last_reply_at < 8:
            return None
        if now - self._last_proactive_at < max(8.0, self.cfg.proactive_interval_sec):
            return None
        cue = self._program_cue()
        if not cue:
            return None
        self._program_spoken.add(key)
        self._remember_topic(str(p.get('topic', '')))
        event = self._proactive_event(cue, now)
        event.program_key = key
        return event

    def program_event_current(self, event):
        key = event.get('program_key') if isinstance(event, dict) else getattr(event, 'program_key', None)
        if key is None:
            generation = (event.get('director_topic_generation') if isinstance(event, dict)
                          else getattr(event, 'director_topic_generation', None))
            return (generation is None or (not self._conversation_topic_paused
                    and generation == self._conversation_topic_generation
                    and not self._program_context.get("live_mode")))
        p = self._program_context
        return bool(p.get('live_mode') and p.get('fresh') and key ==
                    (p.get('show_id'), p.get('card_id'), p.get('stage'), p.get('status_code')))

    def _idle_cue(self) -> str:
        if self._conversation_topic_paused:
            return ""
        if self.phase == "opening":
            cues = ["自然做个温暖的简短问候，让大家知道可以轻松聊天", "问问大家此刻的心情，不要催促或追问"]
        elif self.phase == "interaction":
            cues = ["向全场发起一个二选一小问题", "邀请大家分享今天最想聊的一件事"]
        elif self.phase == "closing":
            cues = ["自然回顾今天的直播并准备道别", "感谢仍在场的观众，温柔地做收尾"]
        elif self._topic_fatigued:
            self._topic_fatigued = False
            topic = self._next_topic_seed()
            return f"刚才的话题已经聊得差不多了，自然转到“{topic}”" if topic else ''
        elif self.current_topic:
            cues = [
                f"继续刚才关于“{self.current_topic}”的话题，补充一个自己的想法",
                f"围绕“{self.current_topic}”问大家一个自然的后续问题",
            ]
        else:
            topic = self._next_topic_seed()
            if not topic:
                return ''
            cues = [f"自然聊起“{topic}”，先分享一点自己的想法再邀请观众接话"]
        cue = cues[self._idle_index % len(cues)]
        self._idle_index += 1
        return cue

    def _proactive_event(self, text: str, now: float) -> Event:
        self._last_proactive_at = now
        event = Event("system", "idle", text, priority=PRIO_IDLE)
        if not self._program_context.get("live_mode"):
            event.director_topic_generation = self._conversation_topic_generation
        return event

    def _directive(self, event: Event) -> str:
        if self._program_context.get('live_mode'):
            p = self._program_context
            return (f"主播选择的栏目：{p.get('topic', '')}；环节：{p.get('stage_label', '')}。"
                    f"真实状态：{p.get('status', '')}。先回应眼前观众，再自然接回主题。"
                    "不改变环节，不替主播发起投票、创建任务或批准上色；这些动作只能等主控回执。"
                    "进度未确认时就说尚未确认，不从旧对话推断已经完成。"
                    "同一个环节的提醒只说一次，后续没有新进展可安静陪伴，不反复问心情或催观众回答。"
                    "不要提到导演、节目单或内部指令。")
        if self._conversation_topic_paused and not self._formal_topic_active():
            return ("用户已结束当前闲聊。只回应眼前明确需要；允许简短确认或安静陪伴，"
                    "不续讲旧话题、不追加问题、不自行另起话题；等待该用户明确开启或恢复。"
                    "不要提及导演或这些内部要求。")
        engagement = self._engagement(self._clock())
        goal = {
            "opening": "建立温暖、安全且没有压力的氛围；简短问候，不追问隐私。",
            "chatting": "先接住当前情绪和需求，再推进话题；合适时只追问一句。",
            "interaction": "先认真回应眼前的人，再向全场抛出一个具体、容易回答且不涉隐私的问题。",
            "highlight": "抓住高能事件做鲜明反应，再接回此前话题，不要立刻降温。",
            "closing": "逐步收束新话题，回顾直播中的具体片段并温柔道别。",
        }[self.phase]
        parts = [
            f"当前节目阶段：{PHASE_LABELS[self.phase]}。本轮目标：{goal}",
            f"当前整体情绪：{self.mood}；场面能量：{self.energy:.2f}。情绪要连续过渡，不要无缘无故突变。",
            f"近况：{engagement['level']}活跃，约{engagement['chat_rate']}条弹幕/分钟，{engagement['viewers']}位近期参与者。",
        ]
        if engagement["level"] == "high":
            parts.append("弹幕较活跃：回复要更短更有节奏，可概括共同话题，不要试图逐条点名。")
        elif engagement["level"] == "low":
            parts.append("当前参与较少：问题要具体而轻松，一次只抛一个，不要制造冷场压力。")
        if self.current_topic:
            parts.append(f"当前话题：{self.current_topic}（已延展{self.topic_turns}轮）。")
        if self.awaiting_audience:
            parts.append(f"仍在等待全场回答的问题：{self.audience_question}。无关弹幕不代表问题已被回答，先回应眼前需要，保留原问题，不要重复催问或换一个问题。")
        elif self.question_pending:
            parts.append(f"上一轮的互动问题仍在等待语音播出：{self.audience_question}。不要重复提出另一个问题。")
        if event.kind == "chat" and _DEVICE_FEEDBACK.search(event.text):
            parts.append("这是设备或播放反馈，优先准确回应具体故障；没有工具回执时，不要声称已调音量或修好。它不是上一轮互动问题的回答。")
        names = self._active_names()
        if names:
            parts.append("近期活跃观众：" + "、".join(names) + "。可以偶尔自然点名，但不要逐个报到。")
        if event.kind == "idle":
            parts.append("这是导演安排的主动发言：直接自然开口，不要说‘没人说话’或暴露导演提示。")
        parts.append("不要提及节目阶段、状态机、导演或这些内部要求。首先准确回应眼前事件。")
        return "\n".join(parts)
