"""Read-only delivery guidance, never a rewrite of the spoken answer.

The model may choose a small wording hesitation when describing an impression.
This conservative prompt policy does not insert filler, randomness, or new facts.
"""
from __future__ import annotations

import re
from collections.abc import Sequence


_REFLECTIVE = re.compile(
    r"感受|感觉|印象|喜欢|偏好|更爱|喜爱|看法|品味|审美|意境|氛围|"
    r"觉得.{0,30}(?:怎么样|如何|好听|好看|适合)|"
    r"(?:怎么|如何|怎样)(?:来|去)?(?:形容|描述|表达)|难以形容|说不上来"
)
_DIRECT = re.compile(
    r"直接说|直接回答|别绕|不要铺垫|别铺垫|"
    r"(?:不要|不用|别).{0,8}(?:语气词|斟酌|犹豫|嗯|我想想|怎么说)|"
    r"多少|几(?:点|号|年|月|日|时|分|秒|次|岁|个)|"
    r"天气|温度|日期|什么时候|哪一年|哪年|价格|预算|播放量|统计|计算|算出|等于|"
    r"发行|发布|首都|人口|海拔|证据|来源|"
    r"查(?:一下|询|到|找)?|搜索|点歌|播放|暂停|停止|删除|开启|关闭|重启|运行|"
    r"安装|配置|修复|报错|代码|版本号|任务|执行|进度|状态|"
    r"救命|求助|危险|受伤|不舒服|生病|吃药|诊断|疼痛|"
    r"\d"
)
# Match the same unmistakable wording hesitations supported by Fish, including
# inline ones. A plain acknowledgement such as “嗯，知道了” does not use up the
# three-reply cooldown; neither does a verb phrase such as “我想一下办法”.
_HESITATION = re.compile(
    r"(?:嗯|唔)(?:…+|\.{3,})|"
    r"(?:该怎么说呢|怎么说呢|让我想一下|让我想想|我想一下|我想想)"
    r"(?=\s*(?:[，,。！？!?；;…]|\.{3,}|$))"
)

DIRECT_GUIDANCE = (
    "本轮口语节奏：直接说清内容，不刻意添加斟酌词。"
    "事实、数字、执行回执和求助先清楚回应；只在确有信息缺口时说明不确定，"
    "不为活人感把可靠结论说得含糊。"
    "必要内容已经说清就停，不猜测对方为什么提问，不为凑句数追加反问或感想。"
)
REFLECTIVE_GUIDANCE = (
    "本轮口语节奏：未明确要求文学创作时，日常感受就用聊天的话回答，不改写成散文。"
    "例如被问‘更喜欢热闹还是安静？’，口语是‘我还是喜欢安静一点，热闹久了会累。’，"
    "不是‘安静像一段被时间收藏的旋律。’示例只学口吻，不复用为本轮内容；"
    "围绕已有细节说清就停，不补未提到的场景、动作或爱好情节。"
    "确有措辞拿捏时，可以任选一次‘嗯……’‘怎么说呢，’‘我想想，’，也可以完全不用。"
    "在斟酌处自然接上具体内容，不必放在开头；"
    "整轮最多一处，不连续叠加，不单独成句或停在语气词上等下一句。"
    "这是措辞节奏，不是在怀疑已知事实，不补造经历、情绪或思考过程。"
)
COOLDOWN_GUIDANCE = (
    "本轮口语节奏：最近三条回复已经用过斟酌词，本轮直接说内容，"
    "不再用‘嗯……’‘怎么说呢’‘我想想’，也不换一个类似口癖补上。"
    "仍可用自然的词序和停顿表达感受，不把事实说得不确定。"
)


def spoken_style_guidance(
    text: str,
    *,
    kind: str = "chat",
    intent: str = "chat",
    recent_replies: Sequence[str] = (),
) -> str:
    """Return constant guidance without mutating history or echoing input.

    Explicit practical requests win even when they also say ``你感觉``.
    Unknown topics simply use the normal direct style. Decisions stay identical
    when prompt previews or token-budget estimates call this more than once.
    """
    clean = str(text or "")[:4000]
    if kind != "chat" or intent not in {"chat", "question"} or _DIRECT.search(clean):
        return DIRECT_GUIDANCE
    if not _REFLECTIVE.search(clean):
        return DIRECT_GUIDANCE
    if any(_HESITATION.search(reply) for reply in list(recent_replies)[-3:]):
        return COOLDOWN_GUIDANCE
    return REFLECTIVE_GUIDANCE
