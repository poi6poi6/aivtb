import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from brain.config import ROOT, WebUIConfig
from brain.minecraft import MinecraftBridge
from brain.webui import WebUI


class MinecraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_invalid_and_disconnected_actions(self):
        bridge = MinecraftBridge(ROOT, asyncio.Queue())
        for action, target in [('shell', ''), ('follow', '../test'), ('wood', '')]:
            with self.assertRaises(ValueError):
                await bridge.command(action, target)
        self.assertIsNone(bridge.process)

    async def test_only_receipts_become_events(self):
        queue = asyncio.Queue()
        bridge = MinecraftBridge(ROOT, queue)
        stream = asyncio.StreamReader()
        for item in [{'type': 'status', 'connected': True}, {'type': 'unknown', 'text': 'fake'},
                     {'type': 'event', 'text': '操作未完成：没有找到树'}]:
            stream.feed_data((json.dumps(item) + '\n').encode())
        stream.feed_eof()
        await bridge._read(SimpleNamespace(stdout=stream))
        self.assertEqual(queue.qsize(), 1)
        event = queue.get_nowait()
        self.assertIn('操作未完成', event.text)
        self.assertIn('失败不能说成功', event.director_context)

    async def test_controls_block_public_and_cross_origin(self):
        for public in (False, True):
            ui = WebUI(WebUIConfig(public_test=public), asyncio.Queue(), '.', {})
            ui.minecraft.command = AsyncMock()
            app = web.Application()
            app.router.add_post('/minecraft-action', ui._minecraft_action)
            async with TestClient(TestServer(app)) as client:
                for origin in ('https://evil.example', str(client.make_url('/')).rstrip('/')):
                    response = await client.post('/minecraft-action', headers={'Origin': origin}, json={'action': 'stop'})
                    self.assertEqual(response.status, 403 if public or origin.startswith('https://evil') else 200)
            if public:
                ui.minecraft.command.assert_not_awaited()

    async def test_bot_process_rejects_disconnected_action_and_closes(self):
        # Protocol smoke test without starting a game server or accepting its EULA.
        if not (ROOT / 'minecraft/node_modules/mineflayer').exists():
            self.skipTest('Minecraft Node dependencies not installed')
        bridge = MinecraftBridge(ROOT, asyncio.Queue())
        try:
            # Spawn the actual process but only send observe; no network connection or world changes.
            from unittest.mock import patch
            original = asyncio.create_subprocess_exec
            async def spawn(*args, **kwargs):
                return await original(*args, **kwargs)
            with patch('brain.minecraft.asyncio.create_subprocess_exec', side_effect=spawn):
                # Launch directly so the normal connect command is not sent.
                bridge.process = await original('node', str(ROOT / 'minecraft/bot.js'),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                bridge.reader = asyncio.create_task(bridge._read(bridge.process))
                await bridge.command('observe')
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if bridge.state.get('error'):
                        break
                self.assertFalse(bridge.state.get('connected'))
                self.assertTrue(bridge.state.get('error'))
                self.assertFalse(any('实际增加' in h for h in bridge.history))
        finally:
            await bridge.close()
        self.assertIsNotNone(bridge.process.returncode)

