import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from brain.tool_registry import ToolRegistry, build_registry


class MusicToolTests(unittest.IsolatedAsyncioTestCase):
    def make_registry(self, live=True):
        ui = SimpleNamespace(
            agent_tasks=None, event_queue=asyncio.Queue(),
            minecraft=SimpleNamespace(state={}, ACTIONS={}, command=AsyncMock()),
            music_requests=SimpleNamespace(submit=lambda *args: None, items=[]),
            _media_search=SimpleNamespace(search=AsyncMock(return_value=[])),
        )
        return ui, build_registry(ui, live=live)

    async def test_music_tools_submit_real_events_without_claiming_playback(self):
        ui, registry = self.make_registry()
        local = await registry.call('music.play_local', {'name':'测试歌曲'}, owner=True)
        self.assertEqual(local['state'], 'accepted')
        event = ui.event_queue.get_nowait()
        self.assertEqual((event.source, event.kind, event.text), ('companion', 'chat', '点歌 测试歌曲'))
        remote = await registry.call('bilibili.play', {'query':'BV1B7411m7LV'}, owner=True)
        self.assertEqual(remote['state'], 'accepted')
        self.assertEqual(ui.event_queue.get_nowait().text, 'B站点歌 BV1B7411m7LV')
        ui._media_search.search.assert_not_awaited()
        self.assertNotIn('已经播放', remote['result'])
        self.assertIn('浏览器助手', remote['result'])
        self.assertIn('等介绍语音完成', remote['result'])

    async def test_guest_and_offline_cannot_enqueue_playback(self):
        for live, owner in [(True, False), (False, True)]:
            ui, registry = self.make_registry(live)
            with self.assertRaises(ValueError):
                await registry.call('bilibili.play', {'query':'BV1B7411m7LV'}, owner=owner)
            self.assertTrue(ui.event_queue.empty())

    async def test_legacy_search_is_unavailable_and_never_calls_upstream_or_queues(self):
        for live in (True, False):
            ui, registry = self.make_registry(live)
            ui._media_search.search.side_effect = AssertionError('Anonymous API must never be called')
            entry = next(item for item in registry.catalog() if item['name'] == 'bilibili.search')
            self.assertFalse(entry['available'])
            self.assertEqual(entry['result_state'], 'unavailable')
            self.assertIn('浏览器点歌助手', entry['description'])
            with self.assertRaisesRegex(ValueError, '控制台连接浏览器点歌助手'):
                await registry.call('bilibili.search', {'query': '海色'}, owner=True)
            with self.assertRaisesRegex(ValueError, '本机主播授权'):
                await registry.call('bilibili.search', {'query': '海色'}, owner=False)
            ui._media_search.search.assert_not_called()
            self.assertTrue(ui.event_queue.empty())

    async def test_registry_no_longer_depends_on_legacy_search_client(self):
        ui, _ = self.make_registry()
        del ui._media_search
        registry = build_registry(ui, live=True)
        result = await registry.call('bilibili.play', {'query': '海色'}, owner=True)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(ui.event_queue.get_nowait().text, 'B站点歌 海色')
        with self.assertRaisesRegex(ValueError, '浏览器点歌助手'):
            await registry.call('bilibili.search', {'query': '海色'}, owner=True)

    async def test_other_unavailable_tools_keep_existing_error_contract(self):
        registry = ToolRegistry()
        handler = AsyncMock()
        registry.add('fixture', 'fixture', {}, handler, available=lambda: False)
        with self.assertRaisesRegex(ValueError, '工具当前不可用，请启动对应服务'):
            await registry.call('fixture', {}, owner=True)
        handler.assert_not_called()
