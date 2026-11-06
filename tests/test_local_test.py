"""Offline console isolation and real HTTP/WS playback protocol regressions."""
import asyncio
import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from brain.local_test import APP_ID, LocalTestWebUI, existing_instance, main, prepare_tone, run


async def receive_until(socket, kind, *, turn_id=None):
    async def read():
        for _ in range(100):
            payload = await socket.receive_json()
            if payload.get('type') == kind and (turn_id is None or payload.get('id', payload.get('turn_id')) == turn_id):
                return payload
        raise AssertionError(f'No {kind} message after 100 payloads')
    return await asyncio.wait_for(read(), 3)


class LocalTestHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'offline'
        # Constructors must not load config/credentials or instantiate an HTTP client.
        with (patch('brain.config.load_settings', side_effect=AssertionError('live config')),
                patch('brain.config.load_dotenv', side_effect=AssertionError('dotenv')),
                patch('aiohttp.ClientSession', side_effect=AssertionError('outbound HTTP'))):
            self.ui = LocalTestWebUI(data_root=self.data, response_delay=.01, slow_delay=.3)
        self.client = TestClient(TestServer(self.ui.make_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.ui.close)
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def connect(self, suffix=''):
        socket = await self.client.ws_connect('/ws' + suffix, origin=self.origin)
        hello = await receive_until(socket, 'hello')
        return socket, hello

    async def test_isolated_storage_and_explicit_offline_hello(self):
        socket, hello = await self.connect()
        async with socket:
            self.assertTrue(hello['local_test'])
            self.assertEqual(hello['llm'], '离线脚本回复')
            self.assertEqual(hello['tts'], 'local-tone')
            self.assertFalse(hello['bilibili'])
            self.assertFalse(hello['mock_danmaku'])
            self.assertFalse(hello['selective_memory'])
            self.assertEqual(self.ui.music_requests.path, self.data / 'data/music/requests.json')
            self.assertEqual(self.ui.suno_manual.root, self.data)
            self.assertEqual(self.ui.minecraft.root, self.data)
            self.assertEqual(list(self.data.rglob('*.sqlite3')), [])
            self.assertEqual([path.relative_to(self.data).as_posix() for path in self.data.rglob('*') if path.is_file()],
                             ['audio/test-tone.wav'])
            status = await (await self.client.get('/local-test-status')).json()
            self.assertEqual(status['app'], APP_ID)
            self.assertTrue(status['ready'])
            page = await self.client.get('/')
            self.assertIn('microphone=()', page.headers['Permissions-Policy'])
            report_response = await self.client.get('/experience-report')
            report = await report_response.json()
            self.assertTrue(report['local_test'])
            self.assertEqual(report['confirmed_playbacks'], 0)
            self.assertIn('未测量扬声器声音', report['measurement'])
            self.assertIn('local-test-experience.json', report_response.headers['Content-Disposition'])

    async def test_reply_plays_real_wav_and_requires_browser_receipt(self):
        socket, _ = await self.connect()
        async with socket:
            await socket.send_json({'type': 'inject', 'text': '你好', 'source': 'companion'})
            event = await receive_until(socket, 'event')
            reply = await receive_until(socket, 'reply', turn_id=event['id'])
            self.assertEqual(reply['mode'], 'local_test')
            self.assertIn('离线脚本', reply['text'])
            audio = await receive_until(socket, 'audio', turn_id=event['id'])
            response = await self.client.get(audio['url'])
            with wave.open(io.BytesIO(await response.read()), 'rb') as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getnframes(), 19200)
            self.assertEqual(self.ui.confirmed_playbacks, 0)
            await socket.send_json({'type': 'audio_started', 'id': audio['id']})
            await receive_until(socket, 'playback')
            await socket.send_json({'type': 'audio_finished', 'id': audio['id'], 'reason': 'ended'})
            await receive_until(socket, 'playback')
            self.assertEqual(self.ui.confirmed_playbacks, 1)
            self.assertIsNone(self.ui.playback.current)

    async def test_unsupported_routes_and_ws_commands_cannot_touch_live_services(self):
        for path in ('/rehearsal', '/tasks', '/tasks-status', '/questions-status', '/shows-status',
                     '/polls-status', '/bilibili-search', '/suno-inbox', '/minecraft-status', '/runtime-status'):
            self.assertEqual((await self.client.get(path)).status, 404, path)
        self.ui.minecraft.command = AsyncMock(side_effect=AssertionError('Minecraft started'))
        with patch.object(self.ui.music_requests, 'cancel', side_effect=AssertionError('music mutation')):
            socket, _ = await self.connect()
            async with socket:
                for action in ('music_cancel', 'music_retry', 'music_play', 'memory_manage', 'set_game_control'):
                    await socket.send_json({'type': action, 'id': 'fixture', 'enabled': True})
                await socket.send_json({'type': 'inject', 'text': '白名单之后仍正常'})
                await receive_until(socket, 'reply')
        self.ui.minecraft.command.assert_not_awaited()
        self.assertFalse(self.ui.music_requests.path.exists())

    async def test_no_external_origin_or_host_and_invalid_scenario_rejected(self):
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/ws', origin='https://elsewhere.example')
        response = await self.client.get('/local-test-status', headers={'Host': 'elsewhere.example'})
        self.assertEqual(response.status, 403)
        for headers in ({}, {'Origin': 'https://elsewhere.example'}):
            response = await self.client.post('/local-test-scenario', json={'scenario': 'reply_error'}, headers=headers)
            self.assertEqual(response.status, 403)
        for payload in ({'scenario': 'unknown'}, {'scenario': []}, [], {'scenario': 'slow', 'url': 'ignored'}):
            response = await self.client.post('/local-test-scenario', json=payload, headers={'Origin': self.origin})
            self.assertEqual(response.status, 200 if isinstance(payload, dict) and payload.get('scenario') == 'slow' else 400)

    async def test_failure_scenarios_are_correlated_and_never_fake_audio(self):
        socket, _ = await self.connect()
        async with socket:
            for scenario, kind in (('reply_error', 'reply_error'), ('tts_error', 'tts_error')):
                response = await self.client.post('/local-test-scenario', json={'scenario': scenario}, headers={'Origin': self.origin})
                result = await response.json()
                self.assertTrue(result['accepted'])
                self.assertIs(type(result['turn_id']), int)
                error = await receive_until(socket, kind, turn_id=result['turn_id'])
                self.assertIn('离线模拟', error['error'])
                self.assertIsNone(self.ui.playback.current)
                self.assertEqual(self.ui.confirmed_playbacks, 0)

    async def test_interrupt_cancels_slow_reply_then_accepts_next_message(self):
        socket, _ = await self.connect()
        async with socket:
            response = await self.client.post('/local-test-scenario', json={'scenario': 'slow'}, headers={'Origin': self.origin})
            result = await response.json()
            await receive_until(socket, 'broadcast_notice')
            await socket.send_json({'type': 'conversation_interrupt'})
            await receive_until(socket, 'reply_cancelled', turn_id=result['turn_id'])
            self.assertIsNone(self.ui.playback.current)
            await socket.send_json({'type': 'inject', 'text': '打断后的新测试'})
            audio = await receive_until(socket, 'audio')
            self.assertNotEqual(audio['turn_id'], result['turn_id'])
            self.assertTrue(all(not worker.done() for worker in self.ui.workers))

    async def test_pause_skip_clear_and_burst_use_real_playback_queue(self):
        socket, _ = await self.connect()
        async with socket:
            for index in range(3):
                await socket.send_json({'type': 'inject', 'source': 'danmaku', 'text': f'测试弹幕 {index}'})
            audio = await receive_until(socket, 'audio')
            # Wait until all three were processed, without depending on wall-clock sleeps.
            for _ in range(100):
                if self.ui.metrics.replies == 3 and len(self.ui.playback.queue) == 2:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.ui.playback.queue), 2)
            await socket.send_json({'type': 'playback_pause', 'paused': True})
            await receive_until(socket, 'audio_control')
            self.assertTrue(self.ui.playback.paused)
            await socket.send_json({'type': 'playback_pause', 'paused': False})
            await receive_until(socket, 'audio_control')
            await socket.send_json({'type': 'playback_skip'})
            next_audio = await receive_until(socket, 'audio')
            self.assertNotEqual(next_audio['id'], audio['id'])
            await socket.send_json({'type': 'playback_clear'})
            await receive_until(socket, 'audio_control')
            self.assertEqual(self.ui.playback.queue, [])
            self.assertIsNone(self.ui.playback.current)

    async def test_existing_instance_reuse_requires_matching_offline_identity(self):
        port = self.client.server.port
        self.assertTrue(await existing_instance(port))
        with patch('brain.local_test.WORKSPACE_ID', 'different-workspace'):
            self.assertFalse(await existing_instance(port))
        self.ui._closing = True
        self.assertFalse(await existing_instance(port))

    async def test_audio_owner_handoff_ignores_observer_receipts_and_reconnects(self):
        control, _ = await self.connect()
        async with control:
            output, _ = await self.connect('?display=1&output=live')
            async with output:
                await control.send_json({'type': 'inject', 'text': '播放归属测试'})
                audio = await receive_until(output, 'audio')
                await control.send_json({'type': 'audio_started', 'id': audio['id']})
                await control.send_json({'type': 'audio_finished', 'id': audio['id'], 'reason': 'ended'})
                # Round-trip a supported action to establish the wrong-owner messages were handled.
                await control.send_json({'type': 'set_manual_mode', 'enabled': True})
                await receive_until(control, 'manual_mode')
                self.assertEqual(self.ui.playback.state, 'dispatched')
                self.assertEqual(self.ui.confirmed_playbacks, 0)
            # Disconnecting the selected output must not claim successful playback.
            for _ in range(100):
                if self.ui.playback.current is None:
                    break
                await asyncio.sleep(.01)
            self.assertIsNone(self.ui.playback.current)
            self.assertEqual(self.ui.confirmed_playbacks, 0)
            await control.send_json({'type': 'inject', 'text': '断开后再试'})
            new_audio = await receive_until(control, 'audio')
            self.assertNotEqual(new_audio['id'], audio['id'])

    async def test_reconnecting_during_slow_reply_restores_generation_state(self):
        socket, _ = await self.connect()
        async with socket:
            response = await self.client.post('/local-test-scenario', json={'scenario': 'slow'}, headers={'Origin': self.origin})
            result = await response.json()
            await receive_until(socket, 'broadcast_notice')
            observer, hello = await self.connect()
            async with observer:
                self.assertEqual(hello['conversation_state'], {
                    'generating': True, 'generating_turn_ids': [result['turn_id']],
                    'synthesizing_turn_ids': [],
                })
                await receive_until(observer, 'reply', turn_id=result['turn_id'])
                self.assertFalse(self.ui.conversation_status()['generating'])

    async def test_run_reuses_existing_instance_without_creating_runtime(self):
        with (patch('brain.local_test.existing_instance', AsyncMock(return_value=True)),
              patch('brain.local_test.LocalTestWebUI') as constructor,
              patch('brain.local_test.webbrowser.open') as browser,
              contextlib.redirect_stdout(io.StringIO())):
            await run(18129, open_browser=False)
            constructor.assert_not_called()
            browser.assert_not_called()

    async def test_occupied_port_closes_only_new_runtime(self):
        runtime = Mock(start=AsyncMock(side_effect=OSError('occupied')), close=AsyncMock())
        with (patch('brain.local_test.existing_instance', AsyncMock(return_value=False)),
              patch('brain.local_test.LocalTestWebUI', return_value=runtime),
              patch('brain.local_test.webbrowser.open') as browser):
            with self.assertRaisesRegex(RuntimeError, 'Existing services were not stopped'):
                await run(18129, open_browser=False)
            runtime.close.assert_awaited_once_with()
            browser.assert_not_called()


class LocalTestStartupTests(unittest.TestCase):
    def test_validate_only_loads_no_credentials_or_data(self):
        with (patch('brain.local_test.LocalTestWebUI', side_effect=AssertionError('created runtime')),
                patch('brain.config.load_settings', side_effect=AssertionError('read config')),
                patch('brain.config.load_dotenv', side_effect=AssertionError('read .env')),
                contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(main(['--validate-only']), 0)

    def test_live_ports_and_invalid_ports_are_rejected(self):
        for port in (0, 80, 8080, 8085, 9880, 9890, 65536):
            with self.subTest(port=port), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--validate-only', '--port', str(port)])

    def test_restart_reuses_one_audio_file_and_tone_does_not_require_tts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = prepare_tone(root)
            audio = first.read_bytes()
            self.assertEqual(prepare_tone(root), first)
            self.assertEqual(first.read_bytes(), audio)
            self.assertEqual([p.name for p in first.parent.iterdir()], ['test-tone.wav'])
