import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import WebUIConfig
from brain.rehearsal import RehearsalService
from brain.webui import WebUI


class RehearsalWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ui = WebUI.__new__(WebUI)
        self.ui.cfg = WebUIConfig()
        self.ui.rehearsal = RehearsalService(lambda report: asyncio.Event().wait())
        self.addAsyncCleanup(self.ui.rehearsal.close)
        app = web.Application()
        app.router.add_get('/rehearsal', self.ui._rehearsal_page)
        app.router.add_get('/rehearsal-status', self.ui._rehearsal_status)
        app.router.add_post('/rehearsal-action', self.ui._rehearsal_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def action(self, data, origin=None):
        return await self.client.post('/rehearsal-action', json=data,
                                      headers={'Origin': origin or self.origin})

    async def test_host_can_start_poll_cancel_and_view_page(self):
        page = await self.client.get('/rehearsal')
        self.assertEqual(page.status, 200)
        self.assertIn('系统没有自动检测', await page.text())
        response = await self.action({'action': 'start'})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result['running'])
        status = await self.client.get('/rehearsal-status')
        self.assertEqual((await status.json())['run_id'], result['run_id'])
        response = await self.action({'action': 'cancel', 'run_id': result['run_id']})
        self.assertEqual((await response.json())['state'], 'cancelled')

    async def test_guest_foreign_origin_and_rebinding_host_are_denied(self):
        response = await self.action({'action': 'start'}, 'https://foreign.invalid')
        self.assertEqual(response.status, 403)
        response = await self.client.get('/rehearsal-status', headers={'Host': 'foreign.invalid'})
        self.assertEqual(response.status, 403)
        self.ui.cfg.public_test = True
        for endpoint in ('/rehearsal', '/rehearsal-status'):
            self.assertEqual((await self.client.get(endpoint)).status, 403)
        self.assertEqual((await self.action({'action': 'start'})).status, 403)
        self.assertFalse(self.ui.rehearsal.running)

    async def test_invalid_json_shapes_and_stale_run_are_rejected(self):
        for data in ([], None, {}, {'action': 'audio_started', 'turn_id': 1},
                     {'action': 'start', 'run_id': 'forged'},
                     {'action': 'confirm', 'run_id': 'old', 'field': 'obs_audio', 'passed': True}):
            response = await self.action(data)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/rehearsal-action', data='{bad',
                                          headers={'Origin': self.origin, 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.ui.rehearsal.running)

    async def test_busy_reason_is_visible_without_creating_run(self):
        self.ui.rehearsal.can_start = lambda: '当前正在播报，请等待队列清空'
        response = await self.action({'action': 'start'})
        self.assertEqual(response.status, 400)
        self.assertIn('播报', (await response.json())['error'])
        self.assertIsNone(self.ui.rehearsal.snapshot()['run_id'])
