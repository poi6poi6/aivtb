"""Real HTTP/WebSocket regressions for local control and audio path boundaries."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from brain.config import WebUIConfig
from brain.webui import WebUI


class WebUIAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'audio'
        self.audio.mkdir()
        self.queue = asyncio.Queue()
        self.mode = asyncio.Event()
        self.memory = Mock()
        self.memory.summary.return_value = {}
        self.memory.list_memories = AsyncMock(return_value=[{
            'id': 1, 'slot': 'viewer:fixture', 'summary': 'fixture memory', 'status': 'active'}])
        self.memory.forget = AsyncMock(return_value=True)
        self.ui = WebUI(WebUIConfig(), self.queue, str(self.audio), {},
                        manual_mode=self.mode, selective_memory=self.memory, workspace_root=self.root)
        self.ui.minecraft.command = AsyncMock()
        self.suno_job = {'id': 'fixture', 'state': 'draft_failed'}
        self.ui.suno_manual.job = Mock(return_value=self.suno_job)
        self.ui.suno_manual.candidates = Mock(return_value=['fixture.wav'])
        self.ui.music_requests.save = Mock()
        @web.middleware
        async def access(request, handler):
            return await self.ui._public_access(request, handler)
        app = web.Application(middlewares=[access])
        app.router.add_get('/ws', self.ui._ws)
        app.router.add_get('/danmaku-ws', self.ui._danmaku_ws)
        app.router.add_get('/audio/{name}', self.ui._audio)
        app.router.add_get('/game-frame.jpg', self.ui._game_frame)
        app.router.add_get('/minecraft-status', self.ui._minecraft_status)
        app.router.add_post('/minecraft-action', self.ui._minecraft_action)
        app.router.add_get('/suno-inbox', self.ui._suno_inbox)
        app.router.add_post('/suno-manual', self.ui._suno_action)
        app.router.add_get('/music-requests', self.ui._music_requests)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def test_http_reads_and_writes_reject_untrusted_host(self):
        self.ui.game_agent = Mock(latest_jpeg=b'fixture-jpeg')
        for host in ('attacker.invalid:8080', 'localhost:70000', 'user@localhost',
                     'localhost/path', 'localhost?query', 'localhost#fragment'):
            headers = {'Host': host, 'Origin': 'http://' + host}
            for path in ('/game-frame.jpg', '/minecraft-status', '/suno-inbox', '/music-requests'):
                with self.subTest(host=host, path=path):
                    response = await self.client.get(path, headers=headers)
                    self.assertEqual(response.status, 403)
            for path, payload in (('/minecraft-action', {'action': 'connect'}),
                                  ('/suno-manual', {'action': 'retry', 'id': 'fixture'})):
                with self.subTest(host=host, path=path):
                    response = await self.client.post(path, headers=headers, json=payload)
                    self.assertEqual(response.status, 403)
        self.ui.minecraft.command.assert_not_awaited()
        self.ui.suno_manual.job.assert_not_called()
        self.ui.suno_manual.candidates.assert_not_called()
        self.assertEqual(self.suno_job['state'], 'draft_failed')

    async def test_http_control_requires_exactly_one_same_origin(self):
        for headers in ({}, {'Origin': 'null'}, {'Origin': self.origin + '/'},
                        {'Origin': self.origin.replace('http:', 'https:')},
                        {'Origin': 'https://foreign.invalid'},
                        [('Origin', self.origin), ('Origin', self.origin)]):
            for path, payload in (('/minecraft-action', {'action': 'connect'}),
                                  ('/suno-manual', {'action': 'retry', 'id': 'fixture'})):
                with self.subTest(headers=headers, path=path):
                    response = await self.client.post(path, headers=headers, json=payload)
                    self.assertEqual(response.status, 403)
        self.ui.minecraft.command.assert_not_awaited()
        self.ui.suno_manual.job.assert_not_called()

    async def test_local_http_reads_and_controls_still_work(self):
        self.ui.game_agent = Mock(latest_jpeg=b'fixture-jpeg')
        response = await self.client.get('/game-frame.jpg')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'fixture-jpeg')
        response = await self.client.get('/suno-inbox')
        self.assertEqual(await response.json(), {'files': ['fixture.wav']})
        headers = {'Origin': self.origin}
        response = await self.client.post('/minecraft-action', headers=headers, json={'action': 'connect'})
        self.assertEqual(response.status, 200)
        self.ui.minecraft.command.assert_awaited_once_with('connect', '')
        response = await self.client.post('/suno-manual', headers=headers,
                                          json={'action': 'retry', 'id': 'fixture'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.suno_job['state'], 'awaiting_draft')
        self.ui.music_requests.save.assert_called_once()

    async def test_http_local_boundary_rejects_nonlocal_peer_and_public_guests(self):
        transport = Mock()
        transport.get_extra_info.return_value = ('192.0.2.50', 1234)
        request = make_mocked_request('GET', '/game-frame.jpg', headers={'Host': 'localhost:8080'},
                                      transport=transport)
        with self.assertRaises(web.HTTPForbidden):
            await self.ui._game_frame(request)
        with self.assertRaises(web.HTTPForbidden):
            await self.ui._suno_inbox(request)
        with self.assertRaises(web.HTTPForbidden):
            self.ui._minecraft_access(request)
        transport.get_extra_info.return_value = ('::1', 1234)
        ipv6 = make_mocked_request('GET', '/game-frame.jpg', headers={'Host': '[::1]:8080'},
                                  transport=transport)
        self.ui._local_access(ipv6)
        self.ui.cfg.public_test = True
        token = 'fixture-invitation-token-over-24-chars'
        with patch.dict(os.environ, {self.ui.cfg.public_token_env: token}):
            headers = {'Cookie': 'vtuber_test=' + token, 'Origin': self.origin}
            for path in ('/game-frame.jpg', '/minecraft-status', '/suno-inbox'):
                self.assertEqual((await self.client.get(path, headers=headers)).status, 403)
            for path in ('/minecraft-action', '/suno-manual'):
                self.assertEqual((await self.client.post(path, headers=headers, json={})).status, 403)
        self.ui.minecraft.command.assert_not_awaited()
        self.ui.suno_manual.job.assert_not_called()

    async def test_foreign_missing_null_and_malformed_origins_rejected_before_upgrade(self):
        for endpoint in ('/ws', '/ws?display=1&output=live', '/danmaku-ws'):
            for headers in ({}, {'Origin': 'https://unrelated.example'}, {'Origin': 'null'},
                            {'Origin': self.origin.replace('http:', 'https:')},
                            {'Origin': '['}, {'Origin': self.origin + '/path'},
                            {'Origin': self.origin + '?query'}, {'Origin': self.origin + '#fragment'},
                            [('Origin', self.origin), ('Origin', self.origin)]):
                with self.subTest(endpoint=endpoint, headers=headers):
                    with self.assertRaises(WSServerHandshakeError) as failure:
                        await self.client.ws_connect(endpoint, headers=headers)
                    self.assertEqual(failure.exception.status, 403)
        self.assertFalse(self.ui._sockets)
        self.assertFalse(self.ui._danmaku_sockets)
        self.memory.list_memories.assert_not_awaited()

    async def test_rebinding_host_and_nonlocal_peer_rejected(self):
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.client.ws_connect('/ws', headers={'Host':'unrelated.example', 'Origin':'http://unrelated.example'})
        self.assertEqual(failure.exception.status, 403)
        transport = Mock()
        transport.get_extra_info.return_value = ('192.0.2.50', 1234)
        request = make_mocked_request('GET', '/ws', headers={
            'Host':'localhost:8080', 'Origin':'http://localhost:8080'}, transport=transport)
        with self.assertRaises(web.HTTPForbidden):
            self.ui._websocket_access(request)
        transport.get_extra_info.return_value = ('::1', 1234)
        ipv6 = make_mocked_request('GET', '/ws', headers={
            'Host':'[::1]:8080', 'Origin':'http://[::1]:8080'}, transport=transport)
        self.assertTrue(self.ui._websocket_access(ipv6))

    async def test_local_control_and_obs_remain_supported(self):
        async with self.client.ws_connect('/ws', origin=self.origin) as control:
            self.assertEqual((await control.receive_json())['type'], 'hello')
            await control.send_json({'type':'memory_manage', 'action':'list'})
            self.assertEqual((await control.receive_json())['memories'][0]['summary'], 'fixture memory')
            await control.send_json({'type':'memory_manage', 'action':'forget', 'id':1})
            self.assertTrue((await control.receive_json())['ok'])
            self.memory.forget.assert_awaited_once_with(1)
            await control.send_json({'type':'set_manual_mode', 'enabled':True})
            await control.receive_json()
            self.assertTrue(self.mode.is_set())
            await control.send_json({'type':'inject', 'text':'fixture chat'})
            event = await asyncio.wait_for(self.queue.get(), 1)
            self.assertTrue(event.owner_control)
            async with self.client.ws_connect('/ws?display=1&output=live', origin=self.origin) as obs:
                self.assertEqual((await obs.receive_json())['type'], 'hello')
                self.assertEqual((await control.receive_json())['action'], 'clear')
                await self.ui.broadcast({'type':'audio', 'id':1})
                self.assertEqual((await obs.receive_json())['id'], 1)

    async def test_local_danmaku_display_is_authorized_but_read_only(self):
        async with self.client.ws_connect('/danmaku-ws', origin=self.origin) as display:
            self.assertEqual((await display.receive_json())['type'], 'history')
            await display.send_json({'type':'set_manual_mode', 'enabled':True})
            await display.send_json({'type':'inject', 'text':'must be ignored'})
            await self.ui.broadcast_danmaku({'text':'fixture'})
            await display.receive_json()
            self.assertFalse(self.mode.is_set())
            self.assertTrue(self.queue.empty())

    async def test_invited_guest_can_chat_but_cannot_manage(self):
        self.ui.cfg.public_test = True
        token = 'fixture-invitation-token-over-24-chars'
        with patch.dict(os.environ, {self.ui.cfg.public_token_env:token}):
            with self.assertRaises(WSServerHandshakeError) as failure:
                await self.client.ws_connect('/ws', origin=self.origin)
            self.assertEqual(failure.exception.status, 403)
            cookie = {'Cookie':'vtuber_test=' + token}
            with self.assertRaises(WSServerHandshakeError):
                await self.client.ws_connect('/ws', headers=cookie, origin='https://unrelated.example')
            async with self.client.ws_connect('/ws', headers=cookie, origin=self.origin) as guest:
                hello = await guest.receive_json()
                self.assertTrue(hello['public_test'])
                self.assertNotIn('selective_memory', hello)
                for payload in ({'type':'memory_manage','action':'forget','id':1},
                                {'type':'set_manual_mode','enabled':True}, {'type':{}}):
                    await guest.send_json(payload)
                await guest.send_json({'type':'inject','source':'companion','text':'fixture chat','user':'spoofed'})
                event = await asyncio.wait_for(self.queue.get(), 1)
                self.assertFalse(event.owner_control)
                self.assertTrue(event.user.startswith('访客-'))
                self.memory.forget.assert_not_awaited()
                self.memory.list_memories.assert_not_awaited()
                self.assertFalse(self.mode.is_set())
                async with self.client.ws_connect('/ws', headers=cookie, origin=self.origin) as second:
                    self.assertEqual((await second.receive_json())['type'], 'room_busy')

    async def test_authenticated_public_https_proxy_remains_supported(self):
        self.ui.cfg.public_test = True
        token = 'fixture-invitation-token-over-24-chars'
        with patch.dict(os.environ, {self.ui.cfg.public_token_env:token}):
            headers = {'Host':'invitation.example', 'Cookie':'vtuber_test=' + token}
            async with self.client.ws_connect('/ws', headers=headers, origin='https://invitation.example') as guest:
                self.assertTrue((await guest.receive_json())['public_test'])

    async def test_valid_audio_and_range_requests_still_work(self):
        content = b'RIFF-fixture-normal-audio'
        (self.audio / '片段.wav').write_bytes(content)
        response = await self.client.get('/audio/片段.wav')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), content)
        response = await self.client.get('/audio/片段.wav', headers={'Range':'bytes=0-3'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b'RIFF')

    async def test_audio_traversal_absolute_paths_and_stream_names_rejected(self):
        content = b'RIFF-fixture-outside-audio'
        outside = self.root / 'outside.wav'
        outside.write_bytes(content)
        (self.audio/'wrong.txt').write_bytes(content)
        from urllib.parse import quote
        for name in ('..\\outside.wav', '../outside.wav', str(outside), 'C:\\outside.wav',
                     '\\\\server\\share\\outside.wav', 'inside.wav:stream.wav', 'wrong.txt', 'missing.wav'):
            with self.subTest(name=name):
                response = await self.client.get('/audio/' + quote(name, safe=''))
                self.assertEqual(response.status, 404)
                self.assertNotEqual(await response.read(), content)

    async def test_resolved_audio_link_outside_root_rejected(self):
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'RIFF-fixture-outside-audio')
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            # Models an existing escaped link without requiring Windows symlink privileges.
            if path == self.audio/'linked.wav':
                return outside
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            response = await self.client.get('/audio/linked.wav')
            self.assertEqual(response.status, 404)
