"""Console reconnect snapshots and audio ownership on fixture-only HTTP servers."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.config import PlaybackConfig, WebUIConfig
from brain.playback import SpeechController, SpeechItem
from brain.webui import WebUI


class ConsoleWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.queue = asyncio.Queue()
        self.manual = asyncio.Event()
        self.playback = SpeechController(PlaybackConfig())
        self.ui = WebUI(WebUIConfig(), self.queue, str(root), {
            'llm': 'fixture-model', 'tts': 'fixture-voice',
            'focus': 'fixture-focus', 'private_folder': 'never-publish-me',
            'selective_memory': {'private': 'fixture memory'},
        }, playback=self.playback, manual_mode=self.manual, workspace_root=root)

        @web.middleware
        async def access(request, handler):
            return await self.ui._public_access(request, handler)

        app = web.Application(middlewares=[access])
        app.router.add_get('/ws', self.ui._ws)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.ui.close)
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def connect(self, suffix='', **kwargs):
        socket = await self.client.ws_connect('/ws' + suffix, origin=self.origin, **kwargs)
        first = await asyncio.wait_for(socket.receive_json(), 1)
        self.assertEqual(first['type'], 'hello', 'the initial snapshot precedes role updates and audio')
        return socket, first

    async def receive(self, socket, kind):
        async def read():
            for _ in range(30):
                message = await socket.receive_json()
                if message['type'] == kind:
                    return message
            raise AssertionError(f'{kind} was not received')
        return await asyncio.wait_for(read(), 1)

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 1)

    def assert_role(self, message, *, owner, target, clients):
        self.assertIs(message['audio_owner'], owner)
        self.assertEqual(message['audio_target'], target)
        self.assertEqual(message['audio_clients'], clients)

    async def test_two_controls_keep_first_owner_and_promote_second_after_close(self):
        first, hello = await self.connect()
        self.assert_role(hello, owner=True, target='control', clients=1)
        second, hello = await self.connect()
        self.assert_role(hello, owner=False, target='control', clients=2)
        self.assert_role(await self.receive(first, 'audio_owner'), owner=True, target='control', clients=2)
        await first.close()
        self.assert_role(await self.receive(second, 'audio_owner'), owner=True, target='control', clients=1)
        await second.close()

    async def test_display_and_obs_upgrade_and_downgrade_roles_for_every_client(self):
        control, _ = await self.connect()
        display, hello = await self.connect('?display=1')
        self.assert_role(hello, owner=True, target='display', clients=2)
        self.assertEqual((await self.receive(control, 'audio_control'))['action'], 'clear')
        self.assert_role(await self.receive(control, 'audio_owner'), owner=False, target='display', clients=2)
        obs, hello = await self.connect('?display=1&output=live')
        self.assert_role(hello, owner=True, target='obs', clients=3)
        self.assertEqual((await self.receive(display, 'audio_control'))['action'], 'clear')
        for socket in (control, display):
            self.assert_role(await self.receive(socket, 'audio_owner'), owner=False, target='obs', clients=3)
        await obs.close()
        self.assert_role(await self.receive(display, 'audio_owner'), owner=True, target='display', clients=2)
        self.assert_role(await self.receive(control, 'audio_owner'), owner=False, target='display', clients=2)
        await display.close()
        self.assert_role(await self.receive(control, 'audio_owner'), owner=True, target='control', clients=1)
        await control.close()

    async def test_closing_observer_changes_count_without_interrupting_current_audio(self):
        control, _ = await self.connect()
        observer, _ = await self.connect()
        await self.receive(control, 'audio_owner')
        self.playback.enqueue(SpeechItem(10, '/audio/fixture.wav', 'fixture', '平静', [], {}, 10, turn_id=7))
        self.playback.audio_started(10)
        self.ui.rehearsal = Mock()
        await observer.close()
        self.assert_role(await self.receive(control, 'audio_owner'), owner=True, target='control', clients=1)
        self.assertEqual(self.playback.current.id, 10)
        self.assertEqual(self.playback.state, 'playing')
        self.ui.rehearsal.playback_disconnected.assert_not_called()
        await control.close()

    async def test_hello_first_contains_fresh_conversation_snapshot_before_audio_redispatch(self):
        self.playback.enqueue(SpeechItem(10, '/audio/fixture.wav', 'fixture', '平静', [], {}, 10, turn_id=7))
        state = {'generating': True, 'turn_id': 8, 'synthesizing_turns': [7]}
        self.ui.conversation_status = lambda: dict(state)
        first, hello = await self.connect()
        self.assertEqual(hello['conversation_state'], state)
        self.assertEqual((await self.receive(first, 'audio'))['id'], 10)
        state.update(generating=False, turn_id=None, synthesizing_turns=[])
        second, hello = await self.connect()
        self.assertEqual(hello['conversation_state'], state)
        self.assert_role(hello, owner=False, target='control', clients=2)
        await second.close()
        await first.close()

    async def test_public_hello_keeps_only_safe_snapshot_fields_and_ignores_obs_priority(self):
        self.ui.cfg.public_test = True
        self.ui.conversation_status = lambda: {'generating': True, 'turn_id': 77, 'synthesizing_turns': []}
        token = 'fixture-console-invitation-over-24-characters'
        with patch.dict(os.environ, {self.ui.cfg.public_token_env: token}):
            guest, hello = await self.connect('?display=1&output=live', headers={'Cookie': 'vtuber_test=' + token})
            self.assertEqual(set(hello), {
                'type', 'public_test', 'llm', 'tts', 'focus', 'playback', 'conversation_state',
                'audio_owner', 'audio_target', 'audio_clients',
            })
            self.assertEqual(hello['conversation_state']['turn_id'], 77)
            self.assert_role(hello, owner=True, target='control', clients=1)
            self.assertNotIn('never-publish-me', str(hello))
            await guest.close()

    async def test_action_allowlist_blocks_management_even_for_owner_but_allows_inject(self):
        self.ui.allowed_ws_actions = frozenset({'inject'})
        self.ui._manage_memory = AsyncMock()
        self.ui._audio_started = AsyncMock()
        self.ui.music_requests.cancel = Mock()
        socket, _ = await self.connect()
        for payload in (
            {'type': 'memory_manage', 'action': 'forget', 'id': 7},
            {'type': 'set_manual_mode', 'enabled': True},
            {'type': 'music_cancel', 'id': 'fixture'},
            {'type': 'audio_started', 'id': 10},
        ):
            await socket.send_json(payload)
        await socket.send_json({'type': 'inject', 'source': 'companion', 'text': 'allowed fixture'})
        event = await asyncio.wait_for(self.queue.get(), 1)
        self.assertEqual(event.text, 'allowed fixture')
        self.assertTrue(event.owner_control)
        self.assertFalse(self.manual.is_set())
        self.ui._manage_memory.assert_not_awaited()
        self.ui._audio_started.assert_not_awaited()
        self.ui.music_requests.cancel.assert_not_called()
        await socket.close()

    async def test_drop_closes_outbox_even_if_socket_was_already_removed(self):
        socket = object()
        outbox = Mock()
        outbox.close = AsyncMock()
        self.ui._outboxes[socket] = outbox
        self.ui._socket_priority[socket] = 2
        self.ui._danmaku_sockets.add(socket)
        await self.ui._drop_socket(socket)
        await self.ui._drop_socket(socket)
        outbox.close.assert_awaited_once()
        self.assertNotIn(socket, self.ui._socket_priority)
        self.assertNotIn(socket, self.ui._danmaku_sockets)

    async def test_drop_closes_popped_outbox_when_playback_cleanup_raises(self):
        socket = object()
        outbox = Mock()
        outbox.close = AsyncMock()
        self.ui._sockets[socket] = None
        self.ui._outboxes[socket] = outbox
        self.ui.rehearsal = Mock()
        self.ui.rehearsal.playback_disconnected.side_effect = RuntimeError('fixture callback failed')
        try:
            await self.ui._drop_socket(socket)
        except RuntimeError:
            pass  # Propagating cleanup errors is allowed; leaking the outbox is not.
        self.assertNotIn(socket, self.ui._sockets)
        self.assertNotIn(socket, self.ui._outboxes)
        outbox.close.assert_awaited_once()
