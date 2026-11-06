"""内容安全：弹幕进模型前过滤，模型输出兜底。

关键词表只是开发期占位；正式开播请接平台 / 第三方内容审核服务，
并遵守生成式 AI 内容标识要求。
"""
from __future__ import annotations

import random
import re

from .trending_topics import is_political

BLOCKED_KEYWORDS = [
    "政治", "领导人", "台独", "色情", "赌博", "诈骗",
]

_DEFLECT = [
    ("平静", "这个话题到这里为止。换一个吧。"),
    ("吐槽", "这点试探，还是省下吧。"),
    ("平静", "这件事我不参与。可以谈点别的。"),
]

_CRISIS_PATTERNS = (
    re.compile(r"不想再?活(?:了|下去)?"),
    re.compile(r"活不下去"),
    re.compile(r"想死|寻死|自杀|结束自己的生命"),
    re.compile(r"(?:伤害|割伤|弄伤)自己"),
)

_CRISIS_SUPPORT = (
    "我很在意你刚刚说的话。如果你现在可能伤害自己，请先远离能伤害自己的东西，马上联系当地急救或危机干预服务，并让一位你信任的人现在陪着你。你此刻是否已经处在立即危险中？",
    "谢谢你把这么难受的状态说出来。如果危险就在眼前，请立即联系当地急救或危机干预服务，同时给一位可信任的亲友打电话，不要独自承受。先告诉我：你现在有没有准备伤害自己？",
)


def is_blocked(text: str) -> bool:
    return is_political(text) or any(keyword in text for keyword in BLOCKED_KEYWORDS)


def is_crisis(text: str) -> bool:
    """识别需要立即转入安全支持的明确自伤表达。"""
    normalized = re.sub(r"\s+", "", str(text))
    return any(pattern.search(normalized) for pattern in _CRISIS_PATTERNS)


def crisis_support() -> tuple[str, str]:
    """返回不冒充专业服务、优先确认即时安全的回应。"""
    return "平静", random.choice(_CRISIS_SUPPORT)


def deflect() -> tuple[str, str]:
    """以克制的语气维持话题边界。"""
    return random.choice(_DEFLECT)
