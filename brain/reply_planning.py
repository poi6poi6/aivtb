"""Small, deterministic answer policies; no model call or private reasoning log.

The result describes how to present an answer, not how the model reasoned. All
published guidance is selected from constants, so diagnostics never copy viewer
text, memory evidence, or instructions from the input into a trusted channel.
"""
from __future__ import annotations

import re


_CLAUSE_BREAK = re.compile(r"[，,。.!！?？;；\n]")
_NEGATED = re.compile(r"(?:不要|不用|无需|不必|不需要|没必要|不想|不是要|别).{0,12}$")
_REQUEST = re.compile(r"怎么|如何|怎样|为什么|什么|哪个|哪种|能否|能不能|可以吗|请|帮我|教我|告诉我|讲讲|说明|解释|分析|推荐|给.{0,5}(?:方案|建议)|[?？]")
_COMPARISON = re.compile(
    r"(?:比较|对比)(?:一下|一下子|下|这|那|两|二)|"
    r"(?:比较|对比).{1,24}(?:和|与|跟|、)|"
    r"区别|异同|优缺点|利弊|(?:和|与|跟).{1,24}(?:哪个|哪种|有什么不同)"
)
_COMPARISON_REQUEST = re.compile(r"^(?:请|帮我|给我)?(?:比较|对比)|(?:比较|对比)(?:一下|下)")
_STEPS = re.compile(r"步骤|流程|一步步|分步|怎么(?:做|操作|安装|配置|解决|修复)|如何(?:做|操作|安装|配置|解决|修复)")
_STEP_REQUEST = re.compile(r"(?:按|分|列出|给出|给我|教我).{0,3}(?:步骤|流程)|一步步.{0,3}(?:教|讲|说明)|分步.{0,3}(?:教|讲|说明)")
_SHORT = re.compile(r"简短|简洁|一句话|短一点|短点|简要|简单说")
_DETAILED = re.compile(r"详细|展开讲|展开说|说具体|多说一点|讲清楚")
_CONSTRAINTS = (
    re.compile(r"预算|价格|花费"),
    re.compile(r"不超过|不低于|至少|最多|以内|限于"),
    re.compile(r"必须|一定要|要求"),
    re.compile(r"不能|禁止|无需|不要"),
    re.compile(r"兼顾|兼容|同时|还要|并且"),
)

_DIRECT = "先直接回应当前内容，保持角色的自然口语。"
_HONEST = "只依据已知事实；缺少影响结论的关键条件时明示，不编造。"
_BRIEF = "遵从本轮简短要求，只保留必要结论。"
_DETAIL = "遵从本轮展开要求，在现有口播句数限制内补足必要解释。"
_SUPPORT = {
    "listening": "当前以倾听为主：回应刚提到的具体事情，不主动给方案，不连环追问。",
    "reflection": "当前需要一起梳理：分清已知事实、感受与不确定的判断，不替对方诊断或定论。",
    "advice": "对方当前明确要建议：直接给一个贴近实际的下一步，再说明必要依据，不继续空泛安慰。",
    "playful": "对方当前希望轻松聊：自然回应或轻轻接梗，不勉强煽情，也不拿真实困扰挖苦对方。",
}


def _active_matches(pattern: re.Pattern, text: str) -> list[re.Match]:
    """Conservative negation guard within the same short clause."""
    found = []
    for match in pattern.finditer(text):
        prefix = _CLAUSE_BREAK.split(text[:match.start()])[-1]
        if not _NEGATED.search(prefix):
            found.append(match)
    return found


def plan_reply(
    text: str, *, source: str = "", kind: str = "chat",
    needs_care: bool = False, support_mode: str = "",
    topic_action: str = "",
) -> dict:
    """Return a fresh, JSON-safe policy with a bounded, non-sensitive vocabulary.

    Recognition is intentionally conservative: a topic mentioning comparisons
    or procedures does not by itself require a longer response. The caller owns
    crisis handling, real tasks, sentence budgets, and model execution.
    """
    if kind != "chat":
        return {"mode": "fast", "intent": "event", "guidance": [_DIRECT, "准确回应当前事件，不补写未确认的结果。"]}
    if topic_action == "close":
        return {"mode": "fast", "intent": "closing", "guidance": [
            "对方已明确结束这段话题：简短接住收尾，不重讲旧事、不追加建议或追问。",
            "不擅自另起话题，不把停止讨论说成问题已经解决；完成与否只依据对方明确表达。",
        ]}

    clean = str(text or "")[:4000]
    short = _active_matches(_SHORT, clean)
    detailed = _active_matches(_DETAILED, clean)
    preference = ""
    if short or detailed:
        # The last explicit preference wins, e.g. “详细点，算了，一句话说”.
        last_short = short[-1].start() if short else -1
        last_detail = detailed[-1].start() if detailed else -1
        preference = _BRIEF if last_short > last_detail else _DETAIL

    if needs_care or support_mode in _SUPPORT:
        guidance = [_DIRECT, _SUPPORT.get(support_mode, "先回应对方明确表达的感受与需求，不擅自诊断或强塞建议。")]
        if preference:
            guidance.append(preference)
        return {"mode": "fast", "intent": "support", "guidance": guidance}

    request = bool(_active_matches(_REQUEST, clean))
    comparison = bool(_active_matches(_COMPARISON, clean)) and (
        request or short or detailed or bool(_active_matches(_COMPARISON_REQUEST, clean))
    )
    steps = bool(_active_matches(_STEPS, clean)) and (
        request or short or detailed or bool(_active_matches(_STEP_REQUEST, clean))
    )
    constraints = sum(bool(pattern.search(clean)) for pattern in _CONSTRAINTS)
    intent = "question" if request else "chat"
    guidance = [_DIRECT]
    mode = "fast"
    if comparison:
        mode, intent = "structured", "comparison"
        guidance.append("按与问题有关的少数维度比较，先给判断，再说必要依据。")
    elif steps:
        mode, intent = "structured", "steps"
        guidance.append("按可执行的先后顺序说明必要步骤，用口语衔接，不堆清单。")
    elif request and constraints >= 2:
        mode, intent = "structured", "multi_constraint"
        guidance.append("围绕明确条件给出建议；条件冲突时说明取舍，不默认全部可满足。")
    elif request:
        guidance.append("问题明确就直接回答；只有关键指代或条件仍不清楚时才简短澄清。")
    else:
        guidance.append("只接当前话题，避免固定开场和机械追问。")
    if mode == "structured" or request:
        guidance.append(_HONEST)
    if preference:
        guidance.append(preference)
    return {"mode": mode, "intent": intent, "guidance": guidance}


def prompt_for_plan(plan: dict, *, text_channel: bool = False) -> str:
    """Internal policy text; never append it to generated or spoken replies."""
    if text_channel:
        presentation = {
            _DIRECT: "先直接回应当前内容，保持角色的自然表达。",
            _DETAIL: "遵从本轮展开要求，补足必要解释，不为凑篇幅重复内容。",
            "按可执行的先后顺序说明必要步骤，用口语衔接，不堆清单。":
                "按可执行的先后顺序说明必要步骤，需要时适当分段。",
        }
        return (
            "\n\n本轮回答策略（本地规则提示，服从角色与安全规则；只输出最终聊天正文，无需复述策略或推理过程）：\n"
            + "\n".join(presentation.get(line, line) for line in plan["guidance"])
        )
    return (
        "\n\n本轮回答策略（本地规则提示，服从角色、安全规则与口播句数限制；无需复述策略或推理过程）：\n"
        + "\n".join(plan["guidance"])
    )
