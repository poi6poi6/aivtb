"""QQ account controls stay local and never leak transport exceptions."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from brain.config import WebUIConfig
from brain.qq_channel import QQError
from brain.webui import WebUI


class QQWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.ui = WebUI(WebUIConfig(), asyncio.Queue(), str(root / 'audio'), {}, workspace_root=root)
        self.state = {'available': True, 'phase': 'offline', 'connected': False, 'owner_bound': False}
        self.service = Mock()
        self.service.snapshot.return_value = self.state
        self.service.action = AsyncMock(return_value=self.state)
        self.ui.qq_channel = self.service
        app = web.Application(client_max_size=32768)
        app.router.add_get('/qq', self.ui._qq_page)
        app.router.add_get('/api/qq', self.ui._qq_status)
        app.router.add_post('/api/qq/action', self.ui._qq_action)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def post(self, data, **kwargs):
        return await self.client.post('/api/qq/action', json=data,
                                      headers={'Origin': self.origin}, **kwargs)

    async def test_page_and_status_do_not_start_qq_and_are_not_cached(self):
        response = await self.client.get('/qq')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('让她认出你', await response.text())
        response = await self.client.get('/api/qq')
        self.assertEqual(await response.json(), self.state)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.service.action.assert_not_awaited()

    async def test_each_explicit_action_reaches_the_service(self):
        for action in ('connect', 'bind', 'disconnect', 'pair', 'unpair', 'reset', 'reply_mode'):
            payload = {'action': action}
            if action == 'pair':
                payload['owner_name'] = '小林'
            elif action == 'reply_mode':
                payload['reply_mode'] = 'voice'
            with self.subTest(action=action):
                response = await self.post(payload)
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), self.state)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.service.action.assert_awaited_with(payload)

    async def test_missing_foreign_and_malformed_origins_never_call_service(self):
        for headers in ({}, {'Origin': 'null'}, {'Origin': 'https://foreign.invalid'},
                        {'Origin': self.origin + '/'}, {'Origin': self.origin.replace('http:', 'https:')},
                        [('Origin', self.origin), ('Origin', self.origin)]):
            with self.subTest(headers=headers):
                response = await self.client.post('/api/qq/action', json={'action': 'bind'}, headers=headers)
                self.assertEqual(response.status, 403)
        self.service.action.assert_not_awaited()

    async def test_nonlocal_or_malformed_host_cannot_read_or_write(self):
        for host in ('evil.invalid', 'localhost:70000', 'u@localhost', 'localhost/path'):
            headers = {'Host': host, 'Origin': 'http://' + host}
            for path in ('/qq', '/api/qq'):
                with self.subTest(host=host, path=path):
                    self.assertEqual((await self.client.get(path, headers=headers)).status, 403)
            self.assertEqual((await self.client.post('/api/qq/action', headers=headers,
                                                    json={'action': 'pair'})).status, 403)
        self.service.snapshot.assert_not_called()
        self.service.action.assert_not_awaited()

    async def test_nonlocal_peer_and_public_mode_cannot_access_handlers(self):
        transport = Mock()
        transport.get_extra_info.return_value = ('192.0.2.50', 1234)
        for method, path, handler in [('GET', '/qq', self.ui._qq_page),
                                     ('GET', '/api/qq', self.ui._qq_status),
                                     ('POST', '/api/qq/action', self.ui._qq_action)]:
            request = make_mocked_request(method, path, transport=transport,
                                          headers={'Host': 'localhost:8080', 'Origin': 'http://localhost:8080'})
            with self.assertRaises(web.HTTPForbidden):
                await handler(request)
        self.ui.cfg.public_test = True
        self.assertEqual((await self.client.get('/qq')).status, 403)
        self.assertEqual((await self.client.get('/api/qq')).status, 403)
        self.assertEqual((await self.post({'action': 'connect'})).status, 403)
        self.service.snapshot.assert_not_called()
        self.service.action.assert_not_awaited()

    async def test_invalid_json_and_action_are_rejected_without_dispatch(self):
        for payload in ([], None, 'bind', {}, {'action': None}, {'action': []}, {'action': 'send'}):
            response = await self.post(payload)
            self.assertEqual(response.status, 400)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response = await self.client.post('/api/qq/action', data='{broken', headers={'Origin': self.origin})
        self.assertEqual(response.status, 400)
        self.service.action.assert_not_awaited()

    async def test_missing_service_reports_unavailable_without_cache(self):
        self.ui.qq_channel = None
        for response in (await self.client.get('/api/qq'), await self.post({'action': 'connect'})):
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('尚未启动', (await response.json())['error'])

    async def test_transport_exceptions_are_sanitized(self):
        self.service.snapshot.side_effect = RuntimeError('private credential: very-secret')
        response = await self.client.get('/api/qq')
        self.assertEqual(response.status, 503)
        self.assertNotIn('very-secret', await response.text())
        for error, status in ((ValueError('very-secret'), 400),
                              (OSError('very-secret'), 503), (RuntimeError('very-secret'), 503)):
            self.service.action.side_effect = error
            response = await self.post({'action': 'connect'})
            self.assertEqual(response.status, status)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertNotIn('very-secret', await response.text())

    async def test_only_fixed_qq_errors_retain_actionable_explanations(self):
        for code, expected in (('busy', '当前回复'), ('not_ready', '依赖'), ('invalid_name', '名字')):
            self.service.action.side_effect = QQError(code)
            response = await self.post({'action': 'pair', 'owner_name': '小林'})
            self.assertEqual(response.status, 400)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn(expected, (await response.json())['error'])
        self.service.action.side_effect = QQError('unknown-secret-token')
        response = await self.post({'action': 'connect'})
        self.assertNotIn('unknown-secret-token', await response.text())


if __name__ == '__main__':
    unittest.main()
