import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.audio_retention import AudioRetention
from brain.config import WebUIConfig
from brain.runtime_supervisor import RuntimeSupervisor
from brain.webui import WebUI


class RuntimeUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.root_patch = patch('brain.webui.ROOT', self.root)
        self.root_patch.start()
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {})
        self.ui.runtime_health = {'ready': True, 'scope': 'main', 'boot_id': 'fixture', 'code': {}}
        self.supervisor = self.ui.runtime_supervisor = RuntimeSupervisor(base_delay=.001)
        self.ui.audio_retention = AudioRetention(self.root)
        app = web.Application()
        for path, handler in [('/runtime-health', self.ui._runtime_health),
                              ('/runtime-status', self.ui._runtime_status),
                              ('/runtime', self.ui._runtime_page),
                              ('/experience-report', self.ui._experience_report),
                              ('/audio-library', self.ui._audio_library)]:
            app.router.add_get(path, handler)
        app.router.add_post('/audio-library-action', self.ui._audio_library_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/')}

    async def asyncTearDown(self):
        await self.supervisor.close()
        await self.client.close()
        await self.ui.close()
        self.root_patch.stop()
        self.folder.cleanup()

    async def test_worker_and_persistence_failures_change_real_health(self):
        healthy = asyncio.Event()
        task = self.supervisor.start('speech', healthy.wait, restart=False)
        self.assertFalse((await (await self.client.get('/runtime-health')).json())['ready'])
        self.ui.runtime_started = True
        response = await (await self.client.get('/runtime-health')).json()
        self.assertTrue(response['ready'])
        self.assertEqual(response['boot_id'], 'fixture')
        self.ui.persistence_status = lambda: {'memory': {'last_error': 'OSError'}}
        self.assertFalse((await (await self.client.get('/runtime-health')).json())['ready'])
        self.ui.persistence_status = lambda: {}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        response = await (await self.client.get('/runtime-status')).json()
        self.assertFalse(response['ready'])
        self.assertEqual(response['workers']['speech']['state'], 'stopped')

    async def test_audio_pin_and_origin_enforcement(self):
        name = '1800000000_1_index.wav'
        (self.root / name).write_bytes(b'fixture')
        response = await (await self.client.get('/audio-library')).json()
        self.assertEqual(response['items'][0]['name'], name)
        self.assertNotIn(str(self.root), str(response))
        data = {'action': 'pin', 'name': name, 'pinned': True}
        self.assertEqual((await self.client.post('/audio-library-action', json=data)).status, 403)
        response = await self.client.post('/audio-library-action', json=data, headers=self.headers)
        self.assertEqual(response.status, 200)
        reopened = AudioRetention(self.root)
        self.assertTrue((await reopened.list_files())[0]['pinned'])
        data['name'] = '../' + name
        self.assertEqual((await self.client.post('/audio-library-action', json=data, headers=self.headers)).status, 400)

    async def test_public_mode_cannot_manage_or_read_runtime_and_audio(self):
        self.ui.cfg.public_test = True
        for route in ['/runtime', '/runtime-status', '/runtime-health', '/audio-library']:
            self.assertEqual((await self.client.get(route)).status, 403)
        response = await self.client.post('/audio-library-action', json={
            'action': 'pin', 'name': '1800000000_1.wav', 'pinned': True}, headers=self.headers)
        self.assertEqual(response.status, 403)

    async def test_recall_diagnostics_stay_private_current_and_outside_metrics(self):
        self.ui.reply_diagnostics = {'turn_id': 7, 'sources': {'memory_recall': {'status': 'ready'}}}
        recall = {'turn_id': 7, 'selected': [{'id': 2, 'summary': 'PRIVATE_FIXTURE_MEMORY'}]}
        self.ui.selective_memory = SimpleNamespace(recall_snapshot=lambda: recall)
        response = await self.client.get('/runtime-status')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('PRIVATE_FIXTURE_MEMORY', str(await response.json()))
        for route in ('/runtime-health', '/experience-report'):
            self.assertNotIn('PRIVATE_FIXTURE_MEMORY', str(await (await self.client.get(route)).json()))
        recall['turn_id'] = 6
        self.assertNotIn('PRIVATE_FIXTURE_MEMORY', str(await (await self.client.get('/runtime-status')).json()))
        recall['turn_id'] = 7
        self.ui.reply_diagnostics['sources']['memory_recall']['status'] = 'timeout'
        self.assertNotIn('PRIVATE_FIXTURE_MEMORY', str(await (await self.client.get('/runtime-status')).json()))
        self.ui.cfg.public_test = True
        self.assertEqual((await self.client.get('/runtime-status')).status, 403)
