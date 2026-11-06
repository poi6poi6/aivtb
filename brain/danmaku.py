"""可选弹幕源：mock 与 B 站连接均由 settings.yaml 显式开启。"""
from __future__ import annotations

import asyncio
import random
import os
import time
import secrets

BILIBILI_STATUS = {'state': 'disabled', 'room_id': 0, 'last_heartbeat': 0}

from .events import PRIO_CHAT, PRIO_GIFT, Event

MOCK_USERS = ["小雨", "阿星", "爱吃鱼的猫", "今晚早点睡", "温水慢慢喝", "柠檬糖", "路过的云"]

MOCK_CHAT = [
    "晚上好，今天终于忙完了",
    "今天有点累，现在只想找个人说说话",
    "你真的是 AI 吗",
    "明天有件重要的事，我有点紧张",
    "今天遇到一件很开心的小事",
    "最近总是睡不好，脑子停不下来",
    "我想养成一个新习惯，但老是坚持不下去",
    "你还记得我之前说过的喜好吗",
    "今天不太想说很多，可以安静待一会儿吗",
    "你会怎么安慰一个对自己很失望的人",
]

MOCK_GIFTS = ["辣条", "小心心", "打call", "这个好哎"]


def bilibili_viewer_key(message) -> str:
    """Use the same account identity for chat, gifts and paid messages."""
    uid = getattr(message, 'uid', 0)
    if uid and str(uid) != '0':
        return f'bili:{uid}'
    uid_hash = getattr(message, 'uid_crc32', '')
    if uid_hash not in (None, '', 0, '0'):
        return f'bili-hash:{uid_hash}'
    # Missing IDs must not merge unrelated viewers merely by their nickname.
    return 'unidentified:' + secrets.token_hex(8)


async def mock_danmaku(
    queue: asyncio.Queue,
    enabled: asyncio.Event | None = None,
    min_interval: float = 2.0,
    max_interval: float = 5.0,
) -> None:
    """按随机间隔产生模拟弹幕，偶尔掉落礼物。"""
    while True:
        if enabled is not None:
            await enabled.wait()
        await asyncio.sleep(random.uniform(min_interval, max_interval))
        # 睡眠期间可能刚被暂停，不再多投递最后一条弹幕。
        if enabled is not None and not enabled.is_set():
            continue
        user = random.choice(MOCK_USERS)
        if random.random() < 0.08:
            gift = f"{random.choice(MOCK_GIFTS)}x{random.randint(1, 5)}"
            await queue.put(Event("danmaku", "gift", gift, user=user, priority=PRIO_GIFT))
        else:
            await queue.put(Event("danmaku", "chat", random.choice(MOCK_CHAT), user=user, priority=PRIO_CHAT))


async def blivedm_danmaku(queue: asyncio.Queue, room_id: int) -> None:
    """连接 B 站直播间，把弹幕 / 礼物 / SC 转成事件。

    需要 `pip install -r requirements-bilibili.txt`。
    """
    try:
        import blivedm
        import blivedm.models.web as web_models  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("未安装官方 blivedm，请先 pip install -r requirements-bilibili.txt") from exc

    class Handler(blivedm.BaseHandler):
        # Bilibili's promotional cards are not viewer chat or gifts.
        _CMD_CALLBACK_DICT = blivedm.BaseHandler._CMD_CALLBACK_DICT.copy()
        _CMD_CALLBACK_DICT['FLOW_REWARD_CARD'] = None

        def _on_heartbeat(self, client, message):
            BILIBILI_STATUS.update(state='connected', last_heartbeat=time.time())

        def _on_danmaku(self, client, message):
            BILIBILI_STATUS.update(state='connected', last_heartbeat=time.time())
            queue.put_nowait(
                Event("danmaku", "chat", message.msg, user=message.uname, priority=PRIO_CHAT,
                      viewer_key=bilibili_viewer_key(message))
            )

        def _on_gift(self, client, message):
            queue.put_nowait(
                Event(
                    "danmaku", "gift",
                    f"{message.gift_name}x{message.num}",
                    user=message.uname, priority=PRIO_GIFT,
                    viewer_key=bilibili_viewer_key(message),
                )
            )

        def _on_super_chat(self, client, message):
            queue.put_nowait(
                Event(
                    "danmaku", "gift",
                    f"SC({message.price}元)：{message.message}",
                    user=message.uname, priority=PRIO_GIFT,
                    viewer_key=bilibili_viewer_key(message),
                )
            )

    import aiohttp
    from http.cookies import SimpleCookie
    session = aiohttp.ClientSession()
    sessdata = os.environ.get('BILIBILI_SESSDATA', '')
    if sessdata:
        cookie = SimpleCookie()
        cookie['SESSDATA'] = sessdata
        cookie['SESSDATA']['domain'] = 'bilibili.com'
        session.cookie_jar.update_cookies(cookie)
    BILIBILI_STATUS.update(state='connecting', room_id=room_id, last_heartbeat=0)
    client = blivedm.BLiveClient(room_id, session=session)
    client.set_handler(Handler())
    client.start()
    try:
        await client.join()
    finally:
        await client.stop_and_close()
        await session.close()
        BILIBILI_STATUS['state'] = 'disconnected'
