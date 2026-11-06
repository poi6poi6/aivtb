"""与具体游戏无关的模拟直播事件源。"""
from __future__ import annotations

import asyncio
import random

from .events import PRIO_AMBIENT, PRIO_CHAT, PRIO_DEATH, PRIO_FIND, Event

GAME_PRIOS = {"death": PRIO_DEATH, "find": PRIO_FIND, "chat": PRIO_CHAT, "ambient": PRIO_AMBIENT}

MOCK_GAME_EVENTS = [
    ("death", "挑战失败，不小心退回了起点"),
    ("death", "差一点就成功，却在最后一步失误了"),
    ("death", "被突如其来的攻击打败了"),
    ("find", "发现了一个稀有道具！"),
    ("find", "解锁了隐藏成就！"),
    ("ambient", "场景进入了夜晚"),
    ("ambient", "背景里突然下起了雨"),
    ("ambient", "来到了一个陌生区域"),
]


async def mock_game(
    queue: asyncio.Queue,
    enabled: asyncio.Event | None = None,
    min_interval: float = 7.0,
    max_interval: float = 13.0,
) -> None:
    while True:
        if enabled is not None:
            await enabled.wait()
        await asyncio.sleep(random.uniform(min_interval, max_interval))
        if enabled is not None and not enabled.is_set():
            continue
        kind, text = random.choice(MOCK_GAME_EVENTS)
        await queue.put(Event("game", kind, text, priority=GAME_PRIOS[kind]))
