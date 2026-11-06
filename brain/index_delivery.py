"""Route the selected IndexTTS2 auditions without changing spoken words.

These are conservative phrase cues, not a general sentiment classifier. Keep
ambiguous, quoted and supportive speech in the original reference delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_LITERAL = re.compile(r'''https?://|www\.|```|[\[\]【】“”‘’"「」『』]''', re.I)
_SETTLED = re.compile(
    r'难受|不舒服|伤心|难过|害怕|恐惧|焦虑|生病|疼|痛|去世|失去|分手|失业|'
    r'累了|疲惫|压力|担心|抱歉|对不起|失败|报错|错误|异常|超时|不可用|没找到|'
    r'没关系|别急|不用急|慢慢(?:说|讲|来)|我在听|我陪你|休息|歇一会|'
    r'让我想|我想一下|我想想|怎么说呢|嗯\s*(?:…|\.{3})'
)
_EXPLANATION = re.compile(
    r'^(?:首先|其次|然后|最后|第一步|第二步|先把|先将|先做|先打开|先点击|先检查|'
    r'原因(?:是|在于)|因为|所以|也就是说|换句话说|举个例子|比如说|'
    r'关键(?:是|在于)|重点(?:是|在于)|最重要的是|具体来说|简单来说|简单说)'
)
_WARM = re.compile(r'舒服|喜欢|好听|耐听|好看|好玩|有意思|挺好|真好|开心|期待|欢迎|太棒')
_CASUAL_TOPIC = re.compile(r'旋律|音乐|这首|这段|这部|这集|这场|曲子|歌曲|故事|电影|游戏|天气|风景|笑话')
_NEGATIVE = re.compile(r'不|没|别|无|难|讨厌|烦')


@dataclass(frozen=True)
class IndexDelivery:
    emotion: str
    alpha: float
    temperature: float | None = None
    mode: str = 'reference'


def plan_index_delivery(text: str, emotion: str, alpha: float, *, base: float,
                        enabled: bool, previous_mode: str | None = None,
                        expressive_teasing: bool = True) -> IndexDelivery:
    """Use B's exact initial targets; the caller smooths consecutive segments.

Only a successful explanation may carry its cadence into the next neutral
segment. An explicit emotion, care cue, new turn or failure ends that carry.
"""
    original = IndexDelivery(emotion, alpha)
    if not enabled:
        return original
    original = IndexDelivery(emotion, alpha, .8)
    if emotion == '吐槽':
        if not expressive_teasing:
            return original
        return IndexDelivery(emotion, round(max(0., min(1., base * (.65 / .60))), 3), .8, 'tease')
    if emotion != '平静' or _LITERAL.search(text) or _SETTLED.search(text):
        return original
    clauses = re.split(r'[，,。；;！？!?\n]', text)
    if any(_EXPLANATION.match(clause.strip()) for clause in clauses):
        return IndexDelivery(emotion, alpha, .9, 'explain')
    # Warm words alone also occur in reassurance ("你告诉我这些，我很开心").
    # Limit this audition to positive comments about a work or shared experience.
    if _CASUAL_TOPIC.search(text) and _WARM.search(text) and not _NEGATIVE.search(text):
        return IndexDelivery('开心', round(max(0., min(1., base * .92)), 3), .8, 'chat')
    if previous_mode == 'explain':
        return IndexDelivery(emotion, alpha, .9, 'explain')
    return original
