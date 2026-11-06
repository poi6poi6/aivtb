"""Parse performance metadata without leaking it into spoken text."""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Collection

DEFAULT_EMOTIONS = frozenset({"开心", "吐槽", "惊讶", "得意", "委屈", "平静"})

AVATAR_ACTIONS = frozenset({
    "挥手", "脸红", "嘟嘴", "弯眼", "弯眼口型", "脸黑", "邪恶眼", "邪恶眼口型",
    "伤心", "生气", "惊讶", "瞳孔地震", "前倾", "手机", "手机点击", "手柄",
    "向左", "向右", "穿外套", "脱外套", "戴眼镜", "摘眼镜", "眼镜1", "眼镜2",
    "短发", "披发", "倾听", "思考", "认可", "疑惑", "轻吐槽", "被夸",
})

_PERFORMANCE_TAG_RE = re.compile(
    r"(?:[【\[]\s*(?P<square>[^】\]\r\n]{1,80})\s*[】\]]"
    r"|[（(]\s*(?P<paren>[^）)\r\n]{1,40})\s*[）)])"
)
_PREFIX_RE = re.compile(r"^(?:动作|表情|情绪|语气)\s*[:：]\s*")
_SPLIT_RE = re.compile(r"[、,，/|\s]+")
_STAGE_DIRECTIONS = frozenset({
    "笑", "微笑", "轻笑", "苦笑", "坏笑", "叹气", "停顿", "沉默", "小声",
    "大声", "温柔", "认真", "害羞", "抽泣", "哭泣", "点头", "摇头",
})


def strip_performance_tags(
    text: str,
    *,
    emotions: Collection[str] = DEFAULT_EMOTIONS,
    actions: Collection[str] = AVATAR_ACTIONS,
) -> tuple[str, list[str]]:
    """Remove known emotion/action/stage tags and return extracted avatar actions.

    Unknown bracketed content is preserved so ordinary content such as ``[来源]``
    is not silently deleted. Explicit metadata prefixes are always removed, while
    only allow-listed avatar actions are returned to the performer.
    """

    emotion_set = set(emotions)
    action_set = set(actions)
    extracted: list[str] = []

    def replace(match: re.Match[str]) -> str:
        raw = (match.group("square") or match.group("paren")).strip()
        normalized = _PREFIX_RE.sub("", raw)
        prefixed = normalized != raw
        items = [item for item in _SPLIT_RE.split(normalized) if item]
        known = bool(items) and all(
            item in emotion_set or item in action_set or item in _STAGE_DIRECTIONS
            for item in items
        )
        if not prefixed and not known:
            return match.group(0)
        for item in items:
            if item in action_set and item not in extracted:
                extracted.append(item)
        return ""

    cleaned = _PERFORMANCE_TAG_RE.sub(replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, extracted


def sanitize_for_tts(text: str) -> str:
    """Defensive cleanup for text reaching any TTS backend."""

    cleaned, _ = strip_performance_tags(text)
    return cleaned


def can_join_ready_speech(left: str, right: str) -> bool:
    """Join only short, complete statements that are already available together.

    The caller owns emotion/action compatibility and the original sentence
    budget. This does not buffer text, rewrite punctuation, or join a question
    or a deliberately early comma clause to its answer/continuation.
    """
    left, right = left.strip(), right.strip()
    return bool(
        left and right and left[-1] in '。！!' and right[-1] in '。！!'
        and not any(mark in left + right for mark in '？?')
        and len(left) <= 24 and len(right) <= 48 and len(left) + len(right) <= 64
    )


class SentenceChunker:
    """Incrementally split generated text into speakable chunks.

    Strong sentence punctuation is emitted immediately. Long punctuation-free
    text is cut near a comma so a streaming TTS pipeline never waits for the
    model's entire answer. An optional opening hesitation stays attached to
    the following clause instead of becoming its own audio request.
    """

    _STRONG_END = frozenset("。！？!?；;\n")
    _SOFT_END = frozenset("，,、：:")
    _SPOKEN_LEADIN_RE = re.compile(
        r"(?:怎么说呢|该怎么说呢|让我想一下|我想一下|让我想想|我想想|嗯嗯|嗯|唔)"
        r"[。！？!?；;，,、：:…\.\s]+"
    )

    def __init__(self, max_chars: int = 42, min_soft_chars: int = 14,
                 first_clause_chars: int = 0, join_spoken_leadins: bool = False) -> None:
        self.max_chars = max(12, max_chars)
        self.min_soft_chars = max(6, min(min_soft_chars, self.max_chars - 2))
        self._buffer = ""
        self.first_clause_chars = max(0, min(60, first_clause_chars))
        self.first_clause_split = False
        self._first = True
        self.join_spoken_leadins = join_spoken_leadins

    def _spoken_leadin_end(self) -> int:
        """Keep one explicit opening hesitation with its following clause.

        Only known performance metadata may precede it. Quoted words and
        ordinary questions must keep their existing sentence boundaries.
        """
        if not self.join_spoken_leadins or not self._first:
            return 0
        start = 0
        while start < len(self._buffer):
            if self._buffer[start].isspace():
                start += 1
                continue
            tag = _PERFORMANCE_TAG_RE.match(self._buffer, start)
            if tag is None or strip_performance_tags(tag.group())[0]:
                break
            start = tag.end()
        leadin = self._SPOKEN_LEADIN_RE.match(self._buffer, start)
        return leadin.end() if leadin else 0

    def feed(self, text: str) -> list[str]:
        if text:
            self._buffer += text
        chunks: deque[str] = deque()
        while self._buffer:
            leadin_end = self._spoken_leadin_end()
            if leadin_end == len(self._buffer):
                break
            # The opening aside does not spend the following clause's budget.
            max_chars = self.max_chars + leadin_end
            # Front-load only the first natural clause, outside performance
            # tags. The caller gives this split one extra transport segment.
            if self._first and self.first_clause_chars:
                depth = 0
                for index, char in enumerate(self._buffer[:max_chars]):
                    if char in '[【（(':
                        depth += 1
                    elif char in ']】）)':
                        depth = max(0, depth - 1)
                    if depth:
                        continue
                    if char in self._STRONG_END and index >= leadin_end:
                        break
                    if char in '，,' and index >= leadin_end:
                        spoken, _ = strip_performance_tags(self._buffer[leadin_end:index])
                        if len(spoken) >= self.first_clause_chars:
                            self.first_clause_split = True
                            chunks.append(self._take(index + 1))
                            break
                if not self._first:
                    continue
            strong = next(
                (index + 1 for index, char in enumerate(self._buffer)
                 if char in self._STRONG_END and index >= leadin_end),
                None,
            )
            if strong is not None:
                chunks.append(self._take(strong))
                continue
            if len(self._buffer) < max_chars:
                break
            window = self._buffer[:max_chars]
            soft_positions = [
                index + 1
                for index, char in enumerate(window)
                if char in self._SOFT_END and index >= leadin_end
                and index + 1 - leadin_end >= self.min_soft_chars
            ]
            chunks.append(self._take(soft_positions[-1] if soft_positions else max_chars))
        return [chunk for chunk in chunks if chunk]

    def flush(self) -> list[str]:
        if not self._buffer.strip():
            self._buffer = ""
            return []
        return [self._take(len(self._buffer))]

    def _take(self, end: int) -> str:
        chunk = self._buffer[:end].strip()
        self._buffer = self._buffer[end:].lstrip()
        self._first = False
        return chunk
