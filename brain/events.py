"""事件：陪同对话 / 弹幕 / 游戏 / 系统内部事件的统一抽象。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 优先级约定：数值越大越值得回应
PRIO_IDLE = 1        # 冷场找话（内部生成）
PRIO_CHAT = 10       # 普通弹幕 / 游戏内聊天
PRIO_AMBIENT = 30    # 游戏日常（刷怪、下雨、迷路）
PRIO_FIND = 70       # 发现物品、达成成就等正向事件
PRIO_GIFT = 80       # 礼物 / SC（>= 此值视为高能事件）
PRIO_DEATH = 100     # 死亡等高能时刻


@dataclass
class Event:
    source: str        # companion | voice | danmaku | game | system | qq_group (isolated channel)
    kind: str          # chat | gift | death | find | ambient | idle | interrupt (控制事件，不送 LLM)
    text: str
    user: str | None = None
    priority: int = PRIO_CHAT
    ts: float = field(default_factory=time.time)
    forced: bool = False  # 测试台手动注入：绕过冷却并保证不被仲裁丢弃
    director_context: str = ""  # 自主直播导演给本次回复的节目目标，不展示给观众
    related_messages: list[dict[str, Any]] = field(default_factory=list)
    corrects: int | None = None
    resume_playback: bool = False
    retract: bool = False
    confirm_memory: bool = False
    viewer_key: str = ''  # Platform identity for scheduling; never spoken as a nickname.
    task_id: str = ''  # Host-populated task reference; never accepted from a chat payload.
    task_revision: int = -1
    task_notice: bool = False
    agent_reply: str = ''
    task_intent_pending: bool = False  # Host-only; semantic lookup runs after attention selection.
