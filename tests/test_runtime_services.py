"""Application ownership, cross-channel attention and independent readiness, offline."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.config import WebUIConfig, PersonaConfig, TTSConfig
from brain.runtime_services import RuntimeServices
from brain.runtime_supervisor import RuntimeSupervisor
from brain.webui import WebUI
from tests.test_runtime_integration import runtime_ui


class RuntimeServicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.queue = asyncio.Queue()
        self.services = RuntimeServices(self.root, self.queue)
        self.addAsyncCleanup(self.services.close)

    async def test_borrowed_webui_close_does_not_stop_application_services(self):
        ui = WebUI(WebUIConfig(), self.queue, str(self.root), {},
                   workspace_root=self.root, services=self.services)
        self.services.bilibili_activity = SimpleNamespace(foreground=Mock(), start=Mock(), close=AsyncMock())
        self.services.bind(ui)
        await self.services.start()
        await ui.close()
        self.assertFalse(self.services._closed)
        self.services.bilibili_activity.close.assert_not_awaited()
        self.services.foreground('qq_private')
        self.services.bilibili_activity.foreground.assert_called_once()
        self.assertIs(ui.music_requests, self.services.music_requests)

    async def test_start_and_close_without_http_and_cleanup_all_after_failure(self):
        self.services.qq_channel = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('fixture')))
        self.services.bilibili_activity = SimpleNamespace(start=Mock(), close=AsyncMock())
        self.services.voice_readiness = SimpleNamespace(start=Mock(), close=AsyncMock(),
            snapshot=lambda: {'ready': False, 'state': 'loading'})
        await self.services.start()
        await self.services.start()
        self.services.bilibili_activity.start.assert_called_once()
        self.services.voice_readiness.start.assert_called_once()
        self.assertFalse(self.services.voice_available())
        with self.assertRaisesRegex(RuntimeError, 'application services'):
            await self.services.close()
        await self.services.close()
        self.services.qq_channel.close.assert_awaited_once()
        self.services.bilibili_activity.close.assert_awaited_once()
        self.services.voice_readiness.close.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await self.services.start()

    async def test_each_foreground_channel_uses_same_preemption(self):
        activity = SimpleNamespace(foreground=Mock(), close=AsyncMock())
        self.services.bilibili_activity = activity
        for source in ('web', 'voice', 'qq_private', 'qq_group'):
            self.services.foreground(source)
        self.assertEqual(activity.foreground.call_count, 4)
        with self.assertRaises(ValueError):
            self.services.foreground('background')
        await self.services.close()
        self.services.foreground('web')
        self.assertEqual(activity.foreground.call_count, 4)

    async def test_public_runtime_cannot_attach_private_channels(self):
        public = RuntimeServices(self.root, self.queue, public_test=True)
        self.addAsyncCleanup(public.close)
        with self.assertRaises(ValueError):
            public.configure_persona(None, None, None)
        self.assertIsNone(public.qq_channel)
        self.assertIsNone(public.bilibili_activity)

    async def test_local_persona_wires_same_companion_to_qq_and_local_ui(self):
        persona = SimpleNamespace(cfg=PersonaConfig(), personality=object(),
                                  activity_completion=AsyncMock(), model_name='deepseek-flash')
        self.services.configure_persona(persona, None, TTSConfig(backend='mock'),
                                        codex_command='fixture-codex')
        self.assertIs(self.services.companion.personality, persona.personality)
        self.assertIs(self.services.qq_channel.companion, self.services.companion)
        self.assertEqual(self.services.companion.vision.command, 'fixture-codex')
        self.assertEqual(self.services.companion.vision.snapshot()['model'], 'gpt-6-astra')
        ui = WebUI(WebUIConfig(), self.queue, str(self.root), {},
                   workspace_root=self.root, services=self.services)
        self.assertIs(ui.companion, self.services.companion)
        self.assertFalse(self.services.private_followups_available())
        persona.activity_completion.assert_not_awaited()

    async def test_health_keeps_text_ready_when_voice_loads_and_redacts_account(self):
        supervisor = RuntimeSupervisor()
        self.addAsyncCleanup(supervisor.close)
        supervisor.start('event_ingest', lambda: asyncio.Event().wait())
        ui = runtime_ui(supervisor)
        state = {'backend': 'indextts2', 'ready': False, 'state': 'loading', 'detail': 'loading'}
        self.services.voice_readiness = SimpleNamespace(snapshot=lambda: dict(state), close=AsyncMock())
        ui.services = self.services
        ui.qq_channel = SimpleNamespace(connected=False)
        ui.bilibili_activity = SimpleNamespace(browser=SimpleNamespace(snapshot=lambda: {
            'connected': True, 'account': {'uid': 'PRIVATE_ACCOUNT', 'name': 'PRIVATE_NAME'}}))
        result = ui.runtime_snapshot()
        self.assertTrue(result['ready'])
        self.assertTrue(result['core_ready'])
        self.assertFalse(result['capabilities']['voice']['ready'])
        self.assertFalse(result['capabilities']['qq']['ready'])
        self.assertTrue(result['capabilities']['bilibili']['ready'])
        self.assertIn('voice_readiness', result['warnings'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        state.update(ready=True, state='ready')
        self.assertNotIn('voice_readiness', ui.runtime_snapshot()['warnings'])
        self.assertTrue(self.services.voice_available())


if __name__ == '__main__':
    unittest.main()
