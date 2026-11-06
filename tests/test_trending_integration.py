"""Public research admission and topic delivery, with no real model/account."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import DirectorConfig, LLMConfig, PersonaConfig, WebUIConfig
from brain.director import LiveDirector
from brain.events import Event
from brain.persona import Persona
from brain.runtime_services import RuntimeServices
from brain.trending_web import install_trending_routes
from brain.webui import WebUI


class TrendingHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.feature = SimpleNamespace(snapshot=Mock(return_value={'state': 'waiting', 'topics': []}),
            update_settings=AsyncMock(), request_refresh=Mock(return_value={'state': 'refreshing'}))
        services = SimpleNamespace(trends=self.feature, bind=lambda ui: None)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), folder.name, {},
                        workspace_root=Path(folder.name), services=services)
        app = web.Application(client_max_size=8192)
        install_trending_routes(self.ui, app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def post(self, path, data, origin=None):
        return await self.client.post(path, json=data, headers={'Origin': origin or self.origin})

    async def test_read_is_inert_manual_refresh_has_no_private_query(self):
        response = await self.client.get('/trends-status')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.feature.request_refresh.assert_not_called()
        response = await self.post('/trends-refresh', {'query': 'PRIVATE_CHAT'})
        self.assertEqual(response.status, 400)
        self.feature.request_refresh.assert_not_called()
        response = await self.post('/trends-refresh', {})
        self.assertEqual(response.status, 202)
        self.feature.request_refresh.assert_called_once_with(manual=True)

    async def test_local_same_origin_boundary_and_public_mode(self):
        for path in ('/trends-settings', '/trends-refresh'):
            self.assertEqual((await self.post(path, {}, 'https://example.org')).status, 403)
            self.assertEqual((await self.client.post(path, json={})).status, 403)
        self.ui.cfg.public_test = True
        self.assertEqual((await self.client.get('/trends-status')).status, 403)
        self.assertEqual((await self.post('/trends-refresh', {})).status, 403)
        self.feature.request_refresh.assert_not_called()
        self.feature.update_settings.assert_not_awaited()

    async def test_settings_error_and_request_size_are_safe(self):
        response = await self.post('/trends-settings', {'enabled': False})
        self.assertEqual(response.status, 200)
        self.feature.update_settings.assert_awaited_once_with({'enabled': False})
        self.feature.update_settings.side_effect = ValueError('PRIVATE_ERROR')
        response = await self.post('/trends-settings', {'categories': ['politics']})
        self.assertEqual(response.status, 400)
        self.assertNotIn('PRIVATE', await response.text())

    async def test_refresh_cooldown_has_accurate_fixed_feedback(self):
        reason = '热点刚更新过或正在等待重试，请稍后再试'
        self.feature.request_refresh.side_effect = ValueError(reason)
        response = await self.post('/trends-refresh', {})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['error'], reason)
        response = await self.post('/trends-refresh', {'padding': 'a' * 4096})
        self.assertEqual(response.status, 413)
        self.feature.request_refresh.side_effect = ValueError('PRIVATE_ERROR')
        response = await self.post('/trends-refresh', {})
        self.assertEqual(response.status, 409)
        self.assertNotIn('PRIVATE', await response.text())


class TopicConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_context_is_shared_but_does_not_leak_into_tarot_or_receipts(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        services = RuntimeServices(Path(folder.name), asyncio.Queue())
        self.addAsyncCleanup(services.close)
        services.trends = SimpleNamespace(context_for=Mock(return_value='PUBLIC_CURRENT_TOPIC'), close=AsyncMock())
        services.bilibili_activity = SimpleNamespace(context_for=Mock(return_value='PUBLIC_BILI'), close=AsyncMock())
        event = Event('qq_group', 'chat', '最近有什么梗')
        self.assertEqual(services.public_context(event), 'PUBLIC_CURRENT_TOPIC\n\nPUBLIC_BILI')
        self.assertTrue(event.trending_topic_context)
        self.assertEqual(services.topic_context(Event('system', 'ambient', '任务完成')), '')
        tarot = Event('companion', 'chat', '帮我抽一张牌')
        tarot.tarot_reading_id = 'fixture'
        self.assertEqual(services.topic_context(tarot), '')
        services.trends.context_for.assert_called_once_with(event.text)

    async def test_director_uses_fresh_seed_only_at_a_new_topic_boundary(self):
        now = [1000.]
        director = LiveDirector(DirectorConfig(enabled=True), clock=lambda: now[0])
        director.phase = 'chatting'
        director.topic_provider = Mock(return_value='一部新番的美术预告')
        director.current_topic = '今天养花的情况'
        self.assertIn('养花', director._idle_cue())
        director.topic_provider.assert_not_called()
        director.current_topic = ''
        self.assertIn('新番', director._idle_cue())
        director.topic_provider.assert_called_once()
        director.clear_conversation_topic()
        now[0] += 600
        self.assertIsNone(director.poll_proactive())
        director.topic_provider.assert_called_once()


class TopicPersonaTests(unittest.IsolatedAsyncioTestCase):
    def persona(self):
        persona = Persona(PersonaConfig(), LLMConfig(), Mock(viewer_note=lambda _: ''))
        persona._chat_backend = SimpleNamespace(available=True, close=AsyncMock())
        persona._research_for = AsyncMock(return_value=None)
        persona.reply_segment_limit = lambda _: 3
        self.addAsyncCleanup(persona.close)
        return persona

    async def test_buffered_hot_topic_cannot_release_a_safe_prefix_before_political_suffix(self):
        persona = self.persona()
        gate, started = asyncio.Event(), asyncio.Event()

        async def stream(*args):
            yield '[平静]这次的新番预告挺有意思。'
            started.set()
            await gate.wait()
            yield '来看看总统选举的候选人。'

        persona._stream_llm = stream
        event = Event('companion', 'chat', '聊聊新番预告吧', user='你')
        event.trending_topic_context = True
        result = []

        async def collect():
            async for segment in persona.stream_reply(event, extra_context='PUBLIC_CURRENT_TOPIC'):
                result.append(segment.text)

        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(result, [])
        gate.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(result)
        self.assertNotIn('新番预告', ''.join(result))
        self.assertNotIn('总统', ''.join(result))
        self.assertEqual(persona.last_mode, 'safety')

    async def test_direct_political_chat_does_not_invoke_model(self):
        persona = self.persona()
        persona._stream_llm = Mock(side_effect=AssertionError('must not call model'))
        result = [part async for part in persona.stream_reply(Event('companion', 'chat', '聊一下总统选举'))]
        self.assertTrue(result)
        persona._stream_llm.assert_not_called()
        self.assertEqual(persona.last_mode, 'safety')

    async def test_topic_material_never_falls_back_to_other_search_engines(self):
        persona = self.persona()
        persona.web_search = SimpleNamespace(search=AsyncMock())
        event = Event('companion', 'chat', '查一下最新新番消息')
        event.trending_topic_context = True
        self.assertIsNone(await Persona._research_for(persona, event))
        persona.web_search.search.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
