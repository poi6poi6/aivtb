"""Local-only checks for the pinned Bilibili transport and viewer identity."""
import asyncio
import hashlib
import json
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from brain.danmaku import bilibili_viewer_key, blivedm_danmaku
from brain.viewer_identity import viewer_identity

try:
    import blivedm
    import brotli
    from blivedm.clients.ws_base import HEADER_STRUCT, Operation, WebSocketClientBase
except ImportError:
    blivedm = None


class BilibiliIdentityTests(unittest.TestCase):
    def test_missing_ids_do_not_merge_same_name_or_null_hash(self):
        for value in ('', '0', 0, None):
            message = SimpleNamespace(uid=0, uid_crc32=value, uname='同名')
            first, second = bilibili_viewer_key(message), bilibili_viewer_key(message)
            self.assertTrue(first.startswith('unidentified:'))
            self.assertNotEqual(first, second)
        self.assertEqual(bilibili_viewer_key(SimpleNamespace(uid=0, uid_crc32='abcd')),
                         'bili-hash:abcd')

    def test_vendored_python_matches_recorded_upstream_source(self):
        root = Path(__file__).resolve().parents[1] / 'vendor/blivedm'
        manifest = json.loads((root / 'upstream.json').read_text('utf-8'))
        for name, expected in manifest['files'].items():
            if name.endswith('.py') or name == 'LICENSE':
                with self.subTest(name=name):
                    self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected)


@unittest.skipUnless(blivedm, 'Optional Bilibili transport is not installed')
class BilibiliTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_gift_and_sc_keep_uid_and_separate_same_names(self):
        class FakeClient:
            def __init__(self, room_id, session):
                self.session = session
            def set_handler(self, handler):
                self.handler = handler
            def start(self):
                pass
            async def join(self):
                for uid in (101, 202):
                    msg = SimpleNamespace(uid=uid, uname='同名', msg='你好',
                                          gift_name='小心心', num=1, price=30, message='留言')
                    self.handler._on_danmaku(self, msg)
                    self.handler._on_gift(self, msg)
                    self.handler._on_super_chat(self, msg)
            async def stop_and_close(self):
                pass

        queue = asyncio.Queue()
        with patch.object(blivedm, 'BLiveClient', FakeClient), patch.dict('os.environ', {'BILIBILI_SESSDATA': ''}):
            await blivedm_danmaku(queue, 1)
        events = [queue.get_nowait() for _ in range(6)]
        for offset, uid in ((0, 101), (3, 202)):
            self.assertEqual([e.viewer_key for e in events[offset:offset + 3]], [f'bili:{uid}'] * 3)
            self.assertEqual(len({viewer_identity(e) for e in events[offset:offset + 3]}), 1)
        self.assertNotEqual(viewer_identity(events[1]), viewer_identity(events[4]))

    async def test_local_websocket_compressed_packets_and_clean_shutdown(self):
        received = asyncio.Queue()
        accepted = asyncio.Event()
        closed = asyncio.Event()

        def packet(body, version=0, operation=Operation.SEND_MSG_REPLY):
            return HEADER_STRUCT.pack(16 + len(body), 16, version, operation, 1) + body

        class Handler(blivedm.HandlerInterface):
            def handle(self, client, command):
                received.put_nowait(command)

        async def websocket(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_bytes()  # Real client auth write.
            await socket.send_bytes(packet(b'{"code":0}', operation=Operation.AUTH_REPLY))
            await socket.receive_bytes()  # Real heartbeat response to auth.
            accepted.set()
            for index, compress, version in ((1, zlib.compress, 2), (2, brotli.compress, 3)):
                payload = packet(json.dumps({'cmd': 'FIXTURE', 'value': index}).encode())
                await socket.send_bytes(packet(compress(payload), version))
            async for _ in socket:
                pass
            closed.set()
            return socket

        application = web.Application()
        application.router.add_get('/ws', websocket)
        async with TestServer(application) as server:
            class LocalClient(WebSocketClientBase):
                async def init_room(self):
                    self._room_id = 1
                    return True
                def _get_ws_url(self, retry_count):
                    return str(server.make_url('/ws').with_scheme('ws'))
                async def _send_auth(self):
                    await self._websocket.send_bytes(self._make_packet({}, Operation.AUTH))

            client = LocalClient(heartbeat_interval=30)
            client.set_handler(Handler())
            client.start()
            try:
                await asyncio.wait_for(accepted.wait(), 2)
                messages = [await asyncio.wait_for(received.get(), 2) for _ in range(2)]
                self.assertEqual([m['value'] for m in messages], [1, 2])
            finally:
                await client.stop_and_close()
            await asyncio.wait_for(closed.wait(), 2)
            self.assertFalse(client.is_running)
            self.assertTrue(client._session.closed)
            self.assertIsNone(client._heartbeat_timer_handle)

    async def test_cookie_and_url_handling_for_shared_session(self):
        async with aiohttp.ClientSession() as session:
            session.cookie_jar.update_cookies({'SESSDATA': 'fixture-only'},
                                             response_url=URL('https://www.bilibili.com/'))
            self.assertEqual(session.cookie_jar.filter_cookies(URL('https://www.bilibili.com/'))['SESSDATA'].value,
                             'fixture-only')
            self.assertNotIn('SESSDATA', session.cookie_jar.filter_cookies(URL('https://unrelated.invalid/')))
            client = blivedm.BLiveClient(1, session=session)
            await client.close()
            self.assertFalse(session.closed)  # Borrowed sessions remain owned by the application.
