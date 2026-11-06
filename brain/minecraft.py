"""Local Mineflayer child process; only observed receipts enter the conversation."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from .events import Event, PRIO_AMBIENT, PRIO_DEATH


class MinecraftBridge:
    ACTIONS = {'connect', 'disconnect', 'stop', 'observe', 'wood', 'planks', 'craft', 'follow', 'auto',
               'campaign', 'campaign_retry', 'eat', 'equip', 'explore', 'home_return', 'water', 'recover', 'respawn'}

    def __init__(self, root: Path, queue: asyncio.Queue):
        self.root, self.queue = root, queue
        self.process = None
        self.reader = None
        self.lock = asyncio.Lock()
        self.state = {'connected': False, 'activity': '未启动', 'busy': False}
        self.history = []

    async def command(self, action: str, target: str = ''):
        if action not in self.ACTIONS:
            raise ValueError('不支持的 Minecraft 操作')
        if action == 'follow' and (not target or len(target) > 16 or not all(c.isascii() and (c.isalnum() or c == '_') for c in target)):
            raise ValueError('请输入有效的游戏玩家名')
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                if action != 'connect':
                    raise ValueError('请先连接 Minecraft')
                logdir = self.root / 'data' / 'minecraft'
                logdir.mkdir(parents=True, exist_ok=True)
                with (logdir / 'bot-error.log').open('ab') as log:
                    self.process = await asyncio.create_subprocess_exec(
                        'node', str(self.root / 'minecraft' / 'bot.js'),
                        cwd=str(self.root / 'minecraft'), stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
                    )
                self.state = {'connected': False, 'activity': '正在连接', 'busy': False}
                self.reader = asyncio.create_task(self._read(self.process))
            self.process.stdin.write((json.dumps({'action': action, 'target': target}) + '\n').encode())
            await self.process.stdin.drain()

    async def _read(self, process):
        try:
            async for raw in process.stdout:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if message.get('type') == 'status':
                    self.state = message
                elif message.get('type') == 'event':
                    text = str(message.get('text', ''))[:600]
                    self.history = (self.history + [text])[-20:]
                    kind = 'death' if message.get('kind') == 'death' else 'ambient'
                    await self.queue.put(Event('game', kind, f'[Minecraft 实际回执] {text}',
                        priority=PRIO_DEATH if kind == 'death' else PRIO_AMBIENT,
                        director_context='只根据本次游戏回执解说。失败不能说成功，不要声称执行了回执以外的游戏操作。'))
        finally:
            if self.process is process:
                self.state = {'connected': False, 'busy': False, 'activity': '连接进程已退出'}

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
