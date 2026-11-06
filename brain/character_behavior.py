"""Short-lived performance state for the formal character, without extra LLM calls.

This is neither a new biography nor viewer memory. It keeps the delivery steady
across adjacent turns, and supplies the same stance to the prompt and performer.
The main scheduler supplies actual work state; text cannot claim a job is running.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from collections.abc import Callable

from . import safety
from .events import Event
from .speech_text import AVATAR_ACTIONS


_INTERESTS = (
    ("花卉", ("花卉", "种花", "养花", "花园", "庭院", "玫瑰", "蔷薇", "郁金香", "绣球", "兰花", "花束")),
    ("衣物", ("衣服", "衣物", "穿搭", "裙子", "外套", "衣橱", "缎带", "礼服")),
    ("旧物", ("旧物", "旧照片", "老照片", "旧书", "古董", "老物件", "纪念品", "故居", "旧日")),
)
_CARE_RE = re.compile(
    r"(?:我|最近|今天|真的|有点|特别|很|好|太).{0,14}(?:难过|难受|焦虑|害怕|失眠|崩溃|委屈|孤独|压力|失恋|被欺负)"
    r"|(?:去世|过世|失去亲人|遭到暴力|遭遇暴力|被家暴|被霸凌|求助|救命)"
    r"|(?:别开玩笑|不要开玩笑|认真听我|认真一点|说正经的|只想倾诉|只想说说|别挖苦|不要挖苦|别嘲讽)"
)
_SHORT_RE = re.compile(r"简短|简洁|一句话|短一点|短点")
_TECHNICAL_RE = re.compile(r"压力测试|(?:进程|程序|软件|系统|游戏)崩溃|崩溃日志")
_DIRECTIVE_RE = re.compile(r"(?:请|能不能|可以|来|给我|帮我|做个|做一个|做一下|换成|换个|换上|戴|摘|穿|脱|笑|挥手|前倾|表演)")
_EXPRESSION_MOODS = {
    "脸红": {"开心"}, "弯眼": {"开心", "得意"}, "弯眼口型": {"开心", "得意"},
    "嘟嘴": {"委屈"}, "伤心": {"委屈"}, "脸黑": {"吐槽"}, "生气": {"吐槽"},
    "邪恶眼": {"吐槽", "得意"}, "邪恶眼口型": {"吐槽", "得意"},
    "惊讶": {"惊讶"}, "瞳孔地震": {"惊讶"},
}
_EXPLICIT_ALIASES = {
    "弯眼": ("笑一下", "笑一笑", "笑脸", "微笑"),
    "弯眼口型": ("笑一下", "笑一笑", "笑脸", "微笑"),
    "穿外套": ("穿上外套", "换上外套"), "脱外套": ("脱掉外套",),
    "戴眼镜": ("戴上眼镜",), "摘眼镜": ("摘掉眼镜", "摘下眼镜"),
}


@dataclass(frozen=True)
class BehaviorTurn:
    interest: str = ""
    care: bool = False
    settling: bool = False
    working: str = ""
    mood: str = "平静"
    explicit_actions: frozenset[str] = frozenset()


class CharacterBehavior:
    """Bounded live-session state. Silence returns it to the calm role baseline."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._working = ""
        self._mood = "平静"
        self._mood_until = 0.0
        self._hold_until = 0.0
        self._care_until = 0.0

    def set_working(self, kind: str = "") -> None:
        # Only a host-observed running drawing task suppresses unsolicited chat.
        self._working = "drawing" if kind == "drawing" else ""

    @property
    def allow_proactive(self) -> bool:
        return not self._working and self._clock() >= self._care_until

    @staticmethod
    def interest_for(event: Event | None) -> str:
        if event is None or event.kind != "chat" or getattr(event, "agent_reply", ""):
            return ""
        return next((label for label, words in _INTERESTS if any(word in event.text for word in words)), "")

    @staticmethod
    def needs_care(event: Event | None) -> bool:
        if event is None or event.kind != "chat":
            return False
        if safety.is_crisis(event.text):
            return True
        if getattr(event, "agent_reply", ""):
            return False
        return bool(_CARE_RE.search(_TECHNICAL_RE.sub("", event.text)))

    @staticmethod
    def _explicit_actions(event: Event | None) -> frozenset[str]:
        if event is None or event.kind != "chat" or not _DIRECTIVE_RE.search(event.text):
            return frozenset()
        return frozenset(
            action for action in AVATAR_ACTIONS
            if action in event.text or any(alias in event.text for alias in _EXPLICIT_ALIASES.get(action, ()))
        )

    def inspect(
        self, event: Event | None, *, care_override: bool | None = None,
        release_care: bool = False,
    ) -> BehaviorTurn:
        """Read-only: prompt previews and token estimates must not advance mood."""
        now = self._clock()
        care = self.needs_care(event) if care_override is None else care_override
        return BehaviorTurn(
            interest="" if care else self.interest_for(event), care=care,
            settling=not release_care and now < self._care_until, working=self._working,
            mood=self._mood if now < self._mood_until else "平静",
            explicit_actions=self._explicit_actions(event),
        )

    def begin(
        self, event: Event, *, care_override: bool | None = None,
        release_care: bool = False,
    ) -> BehaviorTurn:
        care = self.needs_care(event) if care_override is None else care_override
        if care:
            # A real concern wins immediately over a previous cheerful exchange.
            self._care_until = self._clock() + 25.0
            self._mood, self._mood_until, self._hold_until = "平静", 0.0, 0.0
        elif release_care:
            self._care_until = 0.0
        return self.inspect(event, care_override=care_override, release_care=release_care)

    def reset_stance(self) -> None:
        """Drop withdrawn interaction cues while preserving host-owned work."""
        self._mood = "平静"
        self._mood_until = self._hold_until = self._care_until = 0.0

    def context(self, turn: BehaviorTurn) -> str:
        lines = ["本轮角色行为分寸（仅影响表达与表演，不改写正式人设、事实或任务执行结果）："]
        if turn.care:
            lines.append("对方正在认真求助或表达困扰。收起挖苦、得意和玩笑，语气平静，具体接住这件事；不机械套用安慰模板，不擅自诊断，也不强迫对方乐观。")
        elif turn.settling:
            lines.append("此刻仍保持沉静、体贴的表达，温和回应当前观众即可；不提其他观众刚才的事情，不突然切成大笑或毒舌。")
        elif turn.mood != "平静":
            lines.append(f"近期表达仍留有少量“{turn.mood}”的余韵，可以自然收敛，不要随每条弹幕突然切换到相反情绪。实际内容与认真求助始终优先。")
        if turn.interest:
            lines.append(f"当前“{turn.interest}”是观众提到的话题，可以围绕它多展开一个具体细节，或在确有必要时追问；不编造个人经历。")
        if turn.working:
            lines.append("宿主确认绘画任务正在执行。当前注意力主要放在作品上，观众开口仍正常回应；减少闲聊铺垫，不主动另起话题，不声称已画完或编造画面进度。")
        return "\n".join(lines)

    def segment_limit(self, event: Event | None, base: int, *, turn: BehaviorTurn | None = None) -> int:
        turn = turn or self.inspect(event)
        if event is None or _SHORT_RE.search(event.text):
            return base
        # Only one additional sentence for an actual interest; do not grow long
        # room monologues or stretch a one-step wardrobe/action acknowledgement.
        if turn.interest and not (turn.care or turn.settling or turn.working or turn.explicit_actions):
            return min(4, max(base, 2))
        return base

    def perform(self, turn: BehaviorTurn, emotion: str, actions: list[str]) -> tuple[str, list[str]]:
        """Apply the stance to metadata used by both IndexTTS and the avatar.

        Calm is always allowed. Incompatible rapid mood changes go through calm
        instead of carrying an inappropriate smile into a different subject.
        Explicit requested avatar actions bypass only the performance filter;
        this function never invents or executes an action.
        """
        now = self._clock()
        if turn.care or turn.settling:
            emotion = "平静"
        elif emotion != "平静":
            previous = self._mood if now < self._mood_until else "平静"
            same_family = {emotion, previous} <= {"开心", "得意"}
            if previous != "平静" and emotion != previous and not same_family and now < self._hold_until:
                emotion = "平静"
            elif emotion != previous:
                self._mood = emotion
                self._hold_until = now + 18.0
                self._mood_until = now + 60.0

        kept = []
        for action in actions:
            if action in turn.explicit_actions:
                kept.append(action)
            elif action in _EXPRESSION_MOODS and emotion not in _EXPRESSION_MOODS[action]:
                continue
            elif (turn.care or turn.settling) and action in {"挥手", "手机", "手机点击", "手柄"}:
                continue
            elif turn.working and action in {"手机", "手机点击", "手柄"}:
                continue
            else:
                kept.append(action)
        return emotion, kept[:2]
